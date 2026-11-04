# agent_graph — LangGraph 单 Agent 引擎（agent-as-tool）

本包是平台的**唯一 Agent 引擎**：LangChain 1.x + LangGraph 1.x，**单主 Agent + 工具化专家能力**（agent-as-tool 模式）。没有子代理节点、没有 supervisor/规划/DAG 分发——2026-11 破坏式重构删除了旧 `agent/` 自研体系，随后又删除了多 Agent 编排层（decompose/plan/Send fan-out/collect/synthesize 与 9 个 Worker 子图），全部换成工具。

## 图结构（`graph.py`）

```
START → agent（create_agent 主循环）→ END
```

主 Agent 直接挂三类工具：

| 工具 | 实现方式 | 审批 |
|---|---|---|
| `evidence_collector` / `internal_data_provider` / `graph_relation_retriever` | **专家工具**：各自封装一个窄工具循环（`specialists/factory.py`），prompt 来自能力卡片的私有提示词，工具白名单来自卡片 | 无（只读） |
| `risk_analyzer` | 确定性能力：直调 `GraphProviderAdapter.analyze_risk`，无 LLM 循环 | 无（只读） |
| `preview_add_stock` / `preview_adjust_position` / `preview_manual_operation` | 直调 `domain_tools` 预览函数，内部创建待审批 Proposal（plan_id），**不执行** | 无（只产提案） |
| `confirm_pending_proposal` | 调 `writes/executor.execute_control_action` | **interrupt 审批门** |
| `database.write_portfolio_graph_context` / `database.write_evidence_graph_context` | 旧 ToolDefinition 合同直包（`tools/adapter.py`） | **interrupt 审批门** |

## 写审批流

写类工具在主 Agent 循环内抛出 LangGraph `interrupt()`。因为编译后的主 Agent 图**直接作为节点**挂在父图上（`add_node("agent", main_agent)`），中断经父图 checkpointer（Postgres，thread_id=会话）传播到顶层：

1. 运行暂停，返回 `result.requires_confirmation=true`、`plan_id="lg_approve_{run_id}"`；
2. 前端按原界面调 `POST /api/v1/web/agent/pending-actions/{plan_id}/confirm|reject`（confirm 需 `CONFIRM-{后6位}` 短语）；
3. `web_agent_service` → `resume_langgraph_agent` 以 `Command(resume=...)` 恢复——**循环在被中断的工具内部继续执行**，无需重放。

## 目录速览

| 文件/包 | 职责 |
|---|---|
| `service.py` | 入口 `run_langgraph_agent_request` / 审批恢复 `resume_langgraph_agent`；返回前端兼容 result dict |
| `graph.py` | 主图（START→agent→END）+ 初始消息构建 |
| `state.py` | AgentState（messages 通道 + run 元数据） |
| `specialists/` | 三个专家工具 + risk 直连工具（agent-as-tool） |
| `tools/main_tools.py` | 提案预览 / confirm / 图写入直连工具 |
| `tools/adapter.py` | 旧 `ToolDefinition` → LangChain 工具动态包装（JSON Schema→Pydantic；写类过 interrupt） |
| `tools/runtime.py` | `AgentResources`：Neo4j store/identity/provider/impact_service + ToolRegistry |
| `workers/cards.py` | 能力卡片：工具归属元数据与专家私有提示词（单一事实源，无子代理） |
| `toolbox/` | 工具合同（contracts/registry/validation）+ 20 个领域工具实现 |
| `domain_tools/` | 业务工具（预览/提交/查询）+ `dispatch.execute_tool` 精简调度器 |
| `writes/` | 确定性写执行器（Proposal CAS/幂等/复校）+ 12-intent 审批提交网关 |
| `session/` | 会话状态、确认管理、pending plans、Proposal 状态机 |
| `runtime/` | run/steps/tool_calls 记录器、run 快照、artifacts、健康遥测 |
| `memory.py` | LangGraph `PostgresSaver` checkpointer + store（不可用时降级） |
| `trace.py` | trace 事件 + `outputs/agent_runs/<run_id>/flow.md` |
| `trace_adapter.py` | 运行写回 agent_runs/agent_steps/agent_tool_calls（前端运行详情页数据源） |

## 测试

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_agent_graph_engine.py tests/unit/test_agent_graph_writes.py -q
```

13 个离线用例：specialist 工具构建、预览结果脱敏、审批决策解析、general 直答、主循环内调用专家工具、写工具中断→批准后继续执行、拒绝后取消、写执行器/网关契约。
注意：测试用 `_ToolAwareFakeChatModel`（补 `bind_tools`）；specialist 循环用独立的 fake model 实例（`specialist_model` 参数），避免与主循环共享迭代器。

## 手动 E2E

1. 启动 `run_agent_api.py`（8010）+ 前端；
2. 三条路径：普通问答（直答）、业务分析（主 Agent 调专家工具汇总）、写审批（preview 提案 → confirm → interrupt → 面板批准 → 恢复执行）；
3. 对照 `/runs/{run_id}` 运行详情与 `flow.md`。
