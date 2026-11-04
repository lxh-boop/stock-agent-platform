# agent_graph — LangGraph Agent 引擎（唯一引擎）

自 2026-11 破坏式重构起，本包是平台的**唯一 Agent 引擎**（LangChain 1.x + LangGraph 1.x），旧的 `agent/` 自研编排体系（Coordinator/Planner/SpecialistRuntime/Executor/ReAct/Reflection/Handoff/ToolEngine 等）已整体删除，无兼容层、无引擎开关。

同时，原 `agent/` 下被外部依赖的**业务基础模块**已搬迁至本包子包：

| 子包 | 原位置 | 职责 |
|---|---|---|
| `financial_graph/` | `agent/graph/` | Neo4j 金融图谱（store/identity/provider_adapter/impact_service/图写入） |
| `toolbox/` | `agent/tool_runtime/{contracts,registry,validation}` + `agent/worker_tools/` | Worker 工具合同、注册表、校验与 20 个 Worker 私有工具 |
| `workers/cards.py` | `agent/collaboration/worker_directory.py` | 9 个专业 Worker 卡片（单一事实源） |
| `mcp/` | `agent/mcp/` | 内部 data/rag/model MCP server 与准入 |
| `services/` | `agent/services/` | 23 个领域服务（证据检索/模型推理/组合/策略/用户画像…） |
| `domain_tools/` | `agent/tools/` + `agent/tool_engine.py` | 业务工具实现 + `dispatch.execute_tool` 精简调度器（51 工具，替代 1600 行 tool_engine） |
| `session/` | `agent/session/` + `agent/proposals/` | 会话状态、确认管理、pending plans、Proposal 状态机 |
| `runtime/` | `agent/runtime.py` + `agent/artifacts.py` | run/steps/tool_calls 记录器、run 快照、artifact 存储 |
| `writes/` | `agent/collaboration/write_runtime.py` + `write_gateway.py` + 2 个 mutation 适配器 | 确定性写执行器（Proposal CAS/幂等/复校）与 12-intent 审批提交网关 |
| `trace.py` | `agent/console_trace.py` | 精简 trace（控制台 + `outputs/agent_runs/<run_id>/flow.md`） |
| `runtime/health.py` | `agent/runtime_reliability.py` | agent_runs 健康遥测（监控页消费） |

## 图结构（`graph.py`）

```
START → decompose(结构化拆解) → route
  ├─ general → general_answer（直答） → END
  ├─ write   → write_gate（interrupt 审批 → 新写执行器） →（有读请求则继续 plan）
  └─ business(read) → plan → Send 并行分发 provider 阶段 Worker（W01/W02/W03）
        worker → collect
                  ├─ 失败 Worker 重派一次
                  ├─ 派发 consumer 阶段 Worker（W04/W05/W09，携带兄弟结果）
                  ├─ pending_approval → approval_gate（interrupt；批准后带授权重派）
                  └─ 全部完成 → synthesize → END
```

## 入口

- `service.run_langgraph_agent_request(...)`：唯一运行入口，返回前端兼容的 result dict；由 `application/agent_service.py` 调用。
- `service.resume_langgraph_agent(...)`：审批中断后经 pending-actions confirm/reject 恢复（plan_id 形如 `lg_approve_{run_id}`）。

## 审批流

1. 写请求/写工具触发 LangGraph `interrupt()`，运行暂停在 Postgres checkpointer（thread_id=会话）；
2. 前端按原界面调 `POST /api/v1/web/agent/pending-actions/{plan_id}/confirm|reject`（confirm 需 `CONFIRM-{后6位}` 短语）；
3. `web_agent_service` → `resume_langgraph_agent` 以 `Command(resume=...)` 恢复；确定性写执行（CAS claim + preflight + 幂等）在 `writes/`。

## 日志与数据

- `agent_runs / agent_steps / agent_tool_calls / agent_sources / proposals` 等表**沿用原 schema**，由 `runtime/recorder.py` + `trace_adapter.py` 继续写入；历史数据原地保留可查。
- `outputs/agent_runs/<run_id>/flow.md` 轨迹继续产出；旧专属文件日志（react_logs/reflection_logs/message_logs/pending_actions.json）停止产出，磁盘历史保留为存档。

## 测试

```bash
.\.venv\Scripts\python.exe -m pytest tests/unit/test_agent_graph_engine.py tests/unit/test_agent_graph_writes.py -q
```

覆盖：拆解路由、Worker 并行与重派、general 直答、写请求中断→拒绝/批准恢复、Worker 写工具审批后执行、写执行器语义。

## 手动 E2E

1. 启动 `run_agent_api.py`（8010）+ 前端；
2. 走三条路径：普通问答（general 直答）、业务分析（Worker 并行 + 综合报告）、写审批（interrupt → 前端确认短语 → 恢复执行）；
3. 对照 `/runs/{run_id}` 运行详情与 `flow.md`。
