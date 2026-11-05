# GLOSSARY

本仓库核心术语表。只收录需要精确约定含义的领域词，不收录实现细节。

## Agent 侧

| 术语 | 含义 | 不是什么 |
| --- | --- | --- |
| 主 Agent（main agent） | `agent_graph/graph.py` 里的唯一 LLM 循环（`create_agent`）。负责理解用户请求、调用工具、给出最终回答。 | 不是 supervisor，不派发子代理；系统里没有第二个 LLM 循环式"角色"。 |
| 领域工具（domain tool） | 挂在主 Agent 上的只读能力：证据检索、内部数据、图关系、风险计算。权威身份（GraphRef）只能由工具结果产生。 | 不包含提案预览与写操作；也没有"专家工具"（子循环已删）。 |
| 权威引用（GraphRef） | 金融实体的唯一权威身份（node_id / 代码 / kind）。只允许从工具结果中原样复制传递。 | LLM 永远不生成、不猜测、不改写 GraphRef。 |
| 审批门（approval gate） | 官方 `HumanInTheLoopMiddleware`（per_call 模式）对写类工具的声明式拦截：运行暂停在 checkpointer 上，等待用户批准。工具体本身不含审批逻辑。 | 不是旧的两阶段确认令牌流程（那套保留在提交网关，服务模拟盘 REST 提交）。 |
| 中断恢复（resume） | 用户在 pending-actions 面板 confirm/reject 后，以按 interrupt id 的 `Command(resume={"type": "approve"/"reject"})` 在被中断处继续执行。 | 不重放已完成的步骤；也不等于"会话记忆"（记忆复用已删除）。 |
| checkpointer | LangGraph PostgresSaver，thread_id=会话 ID。唯一用途是让中断可跨页面刷新恢复。 | 不是长期记忆，不是对话历史存储（对话历史在前端 finalize 后写入 conversations/messages）。 |
| pending plan | `preview_*` 工具生成的待审批方案（plan_id），存于 pending_actions.json + action_proposals 审计镜像。批准后由写执行器消费。 | 尚无业务效果；`CONFIRM-xxxxxx` 短语只用于 REST 提交路径。 |
| Proposal / canonical proposal | 经 W05 类流程物化的结构化状态变更提案（proposals 三表），带版本与 payload hash。执行走 CAS claim + preflight + 幂等。 | 不是 pending plan（后者是轻量确认计划）。 |

## 数据面

| 术语 | 含义 |
| --- | --- |
| 提交网关（gateway） | `agent_graph/writes/gateway.py`：intent → 提交 adapter 直调（无调度器间接层），服务模拟盘 REST 与审批提交。 |
| 研究文档工具（research_docs_*） | 经外部文件系统 MCP 暴露给主 Agent 的只读工具，读取**人工策展**目录（默认 `data/research`）中的研报/公告/笔记。进入目录即审核动作；工具名一律带 `research_docs_` 前缀并按只读允许清单过滤。 |
| toolbox 工具合同 | `toolbox/worker_tools` 里的 ToolDefinition（JSON Schema + handler），主 Agent 领域读工具与图写入的来源；handler 直调 services。 |

## 已删除概念（见 git 历史，不再有含义）

Coordinator / Planner / Worker DAG / Tool DAG / Send fan-out / Replan /
Specialist Worker / ReAct / Reflection / Handoff / Critic / 会话记忆复用 /
长期记忆 Store / Tool Engine / ToolSpec 注册表 / Skills / 内部 MCP 运输层 /
联网搜索/实时行情类外部 MCP（评估后拒绝，见 ADR 0002）。
