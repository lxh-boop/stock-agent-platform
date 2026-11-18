# agent_graph — LangGraph 单 Agent 引擎（扁平工具直连）

本包是平台的**唯一 Agent 引擎**：LangChain 1.x + LangGraph 1.x，**单主 Agent + 领域工具直连**。没有子代理、没有专家子循环、没有 supervisor/规划/DAG 分发、没有会话记忆复用——旧 `agent/` 自研体系、多 Agent 编排层（decompose/plan/Send fan-out/collect/synthesize 与 9 个 Worker 子图）、专家封装循环（specialists）与会话记忆摘要注入均已删除。

## 图结构（`graph.py`）

```
START → agent（create_agent 主循环）→ END
```

主 Agent 直接挂两类工具：

**领域只读工具**（`tools/main_tools.build_read_tools`，旧 ToolDefinition 合同直包，无审批；另经外部文件系统 MCP 合并 `research_docs_*` 只读工具，读取人工策展的 `data/research` 目录，见 `docs/adr/0003`）：
`evidence_search_news`、`evidence_search_rag`、`internal_prediction_get_stock`、`internal_ranking_get_latest`、`internal_entity_resolve_ranked_security`、`internal_model_get_metrics`、`internal_backtest_get_summary`、`internal_strategy_get_selected`、`internal_portfolio_get_state`、`internal_account_get_state`、`internal_user_profile_get`、`graph_relations_read_neighborhood`、`graph_relations_find_paths`，以及确定性 `risk_analyzer`（直调 `GraphProviderAdapter.analyze_risk`，无 LLM）。

**提案与写入工具**（`tools/main_tools.py`）：

| 工具 | 实现方式 | 审批 |
|---|---|---|
| `preview_add_stock` / `preview_adjust_position` / `preview_manual_operation` | 直调 `domain_tools` 预览函数，内部创建待审批 Proposal（plan_id），**不执行** | 无（只产提案） |
| `confirm_pending_proposal` | 调 `writes/executor.execute_control_action` | **interrupt 审批门** |
| `database.write_portfolio_graph_context` / `database.write_evidence_graph_context` | 旧 ToolDefinition 合同直包（`tools/adapter.py`） | **interrupt 审批门** |

**Strategy 研究工具**（`tools/backtest_tools.py`、`research_data_tools.py`、`research_notes_tools.py`）：

- `list_factor_tables` / `list_factor_fields` / `read_factor_data` 查询本人数据库的实际表、白名单字段和指定日数据；`screen_stock_pool` 返回有日期的代码池。财务、行业和历史指数成分缺失时明确返回不可用。
- `backtest_catalog` 查询研究规格支持的 Alpha158 和日期；`run_strategy_backtest`、`analyze_factors`、`screen_factor_strategy` 经批准提交独立 `backtest.run` 任务，返回 task_id。`get_strategy_backtest` 查询进度及已完成报告，不能把“已提交”称为“已完成”。
- `list_factor_strategies` 读取策略版本；`save_factor_strategy`、`save_strategy_from_report`、`rerun_factor_strategy`、`delete_factor_strategy` 经批准复用策略工作台服务。保存研究策略不启用模拟盘自动交易。
- `read_research_notes` 读取用户保存的研究笔记，内容作为用户资料处理；`save_memory` 在用户明确要求记住、更新或忘记时，经批准写入个人 PostgreSQL。它不自动采集对话、不生成跨会话摘要、不修改账户画像或交易设置。稳定请求 ID 与预期 revision 防止重放和覆盖新内容。

## 写审批流

写类工具在主 Agent 循环内抛出 LangGraph `interrupt()`。因为编译后的主 Agent 图**直接作为节点**挂在父图上（`add_node("agent", main_agent)`），中断经父图 checkpointer 传播到顶层。thread_id 由账号、会话和运行共同派生，避免同一会话的新请求覆盖旧审批：

1. 运行暂停，保存工具参数及 interrupt IDs，返回 `result.requires_confirmation=true`、`plan_id="lg_approve_<sha256>"`。hash 绑定账号、会话、运行和完整中断载荷；同一运行产生下一次中断时使用不同审批 ID；
2. 前端按原界面调 `POST /api/v1/web/agent/pending-actions/{plan_id}/confirm|reject`（confirm 需 `CONFIRM-{后6位}` 短语）；
3. `web_agent_service` 提交持久的 `agent.resume` 后台任务；工作进程复校账号、会话、运行与 checkpoint 的中断载荷，再以 `Command(resume=...)` 恢复。连续点击返回同一任务，数据库锁只覆盖短时状态变更，不覆盖模型等待；
4. 恢复前检查失败可重试；已进入执行但结果不确定时阻止自动重放。审批24小时过期，已完成的决定不会因重复保存 checkpoint 再次变成待批准。

## 目录速览

| 文件/包 | 职责 |
|---|---|
| `service.py` | 入口 `run_langgraph_agent_request` / 审批恢复 `resume_langgraph_agent`；返回前端兼容 result dict |
| `approval_store.py` | PostgreSQL 审批载荷、作用域、24小时有效期及并发幂等锁；不使用本地待审批文件 |
| `graph.py` | 主图（START→agent→END）+ 初始消息构建 |
| `state.py` | AgentState（messages 通道 + run 元数据） |
| `tools/main_tools.py` | 领域只读工具直连（`build_read_tools`）+ 提案预览 / confirm / 图写入 |
| `tools/adapter.py` | 旧 `ToolDefinition` → LangChain 工具动态包装（JSON Schema→Pydantic；写类过 interrupt） |
| `tools/runtime.py` | `AgentResources`：Neo4j store/identity/provider/impact_service + ToolRegistry |
| `workers/cards.py` | 能力卡片：工具归属元数据（`allowed_agent_types`，单一事实源，无子代理） |
| `toolbox/` | 工具合同（contracts/registry/validation）+ 20 个领域工具实现 |
| `domain_tools/` | 业务工具（预览/提交/查询）+ `dispatch.execute_tool` 精简调度器 |
| `writes/` | 确定性写执行器（Proposal CAS/幂等/复校）+ 12-intent 审批提交网关 |
| `session/` | 会话状态、确认管理、pending plans、Proposal 状态机 |
| `runtime/` | run/steps/tool_calls 记录器、run 快照、artifacts、健康遥测 |
| `memory.py` | LangGraph `PostgresSaver` checkpointer，仅服务运行与审批恢复；不是个人研究笔记存储 |
| `tools/external_mcp.py` | 外部 MCP 接入：经 langchain-mcp-adapters 消费官方 filesystem server（人工策展的研究文档目录），失败优雅降级 |
| `trace.py` | trace 事件 + `outputs/agent_runs/<run_id>/flow.md` |
| `trace_adapter.py` | 运行写回 agent_runs/agent_steps/agent_tool_calls（前端运行详情页数据源） |

## 测试

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_agent_graph_engine.py tests/unit/test_agent_graph_writes.py -q
```

离线用例覆盖领域工具直连、预览脱敏、general 直答、主循环工具调用、中断→批准后继续执行、拒绝取消以及写执行器契约；策略研究工作流另外覆盖真实 PostgreSQL 账号隔离、并发决定、请求重放和版本关联。
注意：测试用 `_ToolAwareFakeChatModel`（补 `bind_tools`）。

## 手动 E2E

1. 启动 `run_agent_api.py`（8010）+ 前端；
2. 三条路径：普通问答（直答）、业务分析（主 Agent 直接调领域工具汇总）、写审批（preview 提案 → confirm → interrupt → 面板批准 → 恢复执行）；
3. 对照 `/runs/{run_id}` 运行详情与 `flow.md`。
