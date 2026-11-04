# agent_graph — LangChain/LangGraph 引擎（重构版）

本包是自研 Agent（`agent/` 目录的 Coordinator/Planner/SpecialistRuntime 体系）的 **LangChain 1.x + LangGraph 1.x 平行重建**。
业务逻辑（RAG 检索、Neo4j 图谱、内部数据查询、模拟盘写执行）全部复用现有模块；编排、LLM 调用、工具定义、会话记忆改用 LangChain/LangGraph 官方原语。

## 引擎切换

默认引擎仍是 `legacy`（旧自研 runtime），切换入口二选一：

```bash
# 方式一：环境变量
set STOCK_AGENT_ENGINE=langgraph

# 方式二：local_app_config.json
{ "agent_engine": "langgraph" }
```

生效位置：`application/agent_service.py::AgentApplicationService.run()` 按 `agent_graph.service.resolve_agent_engine()` 分流。
FastAPI 接口、Task API/SSE、前端、数据库 schema 均不变；新引擎返回与 `agent/executor.py::run_agent_request` 相同形状的 dict。

## 图结构（agent_graph/graph.py）

```
START → decompose(结构化拆解) → route
  ├─ general → general_answer（直答） → END
  ├─ write   → write_gate（interrupt 审批） →（有读请求则继续 plan）→ synthesize → END
  └─ business(read) → plan → Send 并行分发 provider 阶段 Worker（W01/W02/W03）
        worker → collect
                  ├─ 失败 Worker 重派一次（legacy max_replans=1 语义）
                  ├─ 派发 consumer 阶段 Worker（W04/W05/W09，携带兄弟结果）
                  ├─ pending_approval → approval_gate（interrupt；批准后带授权重派）
                  └─ 全部完成 → synthesize → END
```

与自研架构的差异（预期行为变化）：
- 放弃两层 DAG 预规划（Worker 内 Tool DAG 由 `create_agent` 的工具循环自主决定）；
- Worker 依赖简化为 provider→consumer 两阶段批量，而非任意 DAG。

## 模块速览

| 文件 | 职责 |
|---|---|
| `service.py` | 对外入口 `run_langgraph_agent_request` / 审批恢复 `resume_langgraph_agent` / 引擎开关 |
| `graph.py` | 主 StateGraph：decompose/plan/worker/collect/approval_gate/write_gate/synthesize |
| `state.py` | AgentState（worker_results 用 add-reducer 承接并行 Send） |
| `schemas.py` | Decomposition / WorkerPlan 结构化输出（LLM 只产自然语言实体，禁止产权威 ID） |
| `llm.py` | `ChatOpenAI` 工厂（复用 `LLMRuntimeSettings`；Qwen/DeepSeek extra_body、ZAI Responses API 语义保留） |
| `tools/runtime.py` | `AgentResources`：Neo4j store/identity/provider/impact_service + ToolRegistry（coordinator 同款初始化模板） |
| `tools/adapter.py` | 把旧 `ToolDefinition` 动态包装成 LangChain 工具（JSON Schema→Pydantic；写类工具过 `interrupt()` 审批） |
| `tools/registry.py` | Worker 卡片 → 工具白名单绑定（复用 `CapabilityWorkerDirectory`） |
| `workers/factory.py` | `create_agent` 按 Worker 卡片构建子图（9 张卡片直接 import 旧目录，单一事实源） |
| `approval.py` | 审批 payload / resume 决策解析 |
| `memory.py` | LangGraph `PostgresSaver` + `PostgresStore` 装配（runtime schema；不可用时降级为无检查点运行） |
| `trace_adapter.py` | 把运行写入 `agent_runs`/trace 事件，保住 `/runs/{run_id}` 前端页面 |
| `prompts/` | 中文 prompt（迁移自 request_bundle/planner/coordinator，保留合规免责声明） |

## 审批流（pending-actions 兼容）

1. 写类工具/写请求触发 `interrupt()`，运行暂停在检查点上，返回 `result.requires_confirmation=true`、`result.plan_id="lg_approve_{run_id}"`；
2. 前端按原流程调 `POST /api/v1/web/agent/pending-actions/{plan_id}/confirm|reject`；
3. `web_agent_service.control_pending_action` 识别 `lg_approve_` 前缀 → `resume_langgraph_agent` 以 `Command(resume=...)` 恢复（confirm 仍需 `CONFIRM-{后6位}` 短语，与旧流程一致）。

## 测试

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_agent_graph_engine.py -q
```

9 个用例覆盖：工具名映射/参数模型、审批决策解析、引擎开关、general 直答、业务流规划与执行、失败重派、写请求中断与拒绝恢复、Worker 写工具中断→批准后执行。全部离线（FakeChatModel + InMemorySaver）。

## 手动 E2E（切引擎前建议）

1. 启动后端 `run_agent_api.py`（默认 8010）+ 前端；
2. `STOCK_AGENT_ENGINE=langgraph` 后走三条路径：
   - 普通问答（"你好"）→ general 直答；
   - 业务分析（"分析最新模型排名"）→ W02/W01 并行 + 综合报告；
   - 写审批（"确认执行调仓方案"）→ 中断 → 前端确认短语 → 恢复执行。
3. 对照 `/runs/{run_id}/trace` 页面确认 trace 适配层输出。

## 后续（本次未做）

- MCP（data/rag/model server）改接 `langchain-mcp-adapters`；
- 删除旧 `agent/` 自研编排与 legacy 第一层 38 工具（切换稳定后单独立项）；
- trace 适配层增强（逐步补齐 lifecycle/计时事件粒度）。
