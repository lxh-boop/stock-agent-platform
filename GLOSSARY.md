# GLOSSARY

本仓库核心术语表。只收录需要精确约定含义的领域词，不收录实现细节。

## Agent 侧

| 术语 | 含义 | 不是什么 |
| --- | --- | --- |
| 主 Agent（main agent） | `agent_graph/graph.py` 里的唯一 LLM 循环（`create_agent`）。负责理解用户请求、调用工具、给出最终回答。 | 不是 supervisor，不派发子代理；系统里没有第二个 LLM 循环式"角色"。 |
| 领域工具（domain tool） | 挂在主 Agent 上的只读能力：证据检索、内部数据、图关系、风险计算。权威身份（GraphRef）只能由工具结果产生。 | 不包含提案预览与写操作；也没有"专家工具"（子循环已删）。 |
| 权威引用（GraphRef） | 金融实体的唯一权威身份（node_id / 代码 / kind）。只允许从工具结果中原样复制传递。 | LLM 永远不生成、不猜测、不改写 GraphRef。 |
| 审批门（approval gate） | 写类工具内的 LangGraph `interrupt()`：运行暂停在 checkpointer 上，等待用户批准。 | 不是旧的两阶段确认令牌流程（那套保留在提交网关，服务模拟盘 REST 提交）。 |
| 中断恢复（resume） | 用户在 pending-actions 面板 confirm/reject 后，以 `Command(resume=...)` 在被中断的工具内部继续执行。 | 不重放已完成的步骤；也不等于"会话记忆"（记忆复用已删除）。 |
| checkpointer | LangGraph PostgresSaver，thread_id=会话 ID。唯一用途是让中断可跨页面刷新恢复。 | 不是长期记忆，不是对话历史存储（对话历史在前端 finalize 后写入 conversations/messages）。 |
| pending plan | `preview_*` 工具生成的待审批方案（plan_id），存于 pending_actions.json + action_proposals 审计镜像。批准后由写执行器消费。 | 尚无业务效果；`CONFIRM-xxxxxx` 短语只用于 REST 提交路径。 |
| Proposal / canonical proposal | 经 W05 类流程物化的结构化状态变更提案（proposals 三表），带版本与 payload hash。执行走 CAS claim + preflight + 幂等。 | 不是 pending plan（后者是轻量确认计划）。 |

## 数据面

| 术语 | 含义 |
| --- | --- |
| dispatch（execute_tool） | `agent_graph/domain_tools/dispatch.py`：10 个提交/预览工具的确定性调度器，服务提交网关与模拟盘 REST 预览。主 Agent 的读工具**不**经过它。 |
| toolbox 工具合同 | `toolbox/worker_tools` 里的 20 个 ToolDefinition（JSON Schema + handler），主 Agent 读工具与图写入的来源。 |
| MCP stdio 链 | `internal.*`/`evidence.*` 读工具的真实数据面：每次调用 spawn data/rag/model server 子进程 → services。已知运输层成本，见 ADR 0001。 |

## 已删除概念（见 git 历史，不再有含义）

Coordinator / Planner / Worker DAG / Tool DAG / Send fan-out / Replan /
Specialist Worker / ReAct / Reflection / Handoff / Critic / 会话记忆复用 /
长期记忆 Store / Tool Engine / ToolSpec 注册表 / Skills。
