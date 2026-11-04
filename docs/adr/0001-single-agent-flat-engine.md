# ADR 0001：单主 Agent（扁平领域工具直连）作为唯一引擎

- 状态：已接受（2026-11）
- 背景文档：`agent_graph/README.md`

## 背景

项目历史上存在两代 Agent 实现：

1. **自研多 Agent 运行时**（已删除，git 历史 `0658844` 之前）：`agent/` 目录下的
   Coordinator / Planner / SpecialistRuntime / 两层 Tool DAG / ReAct / Reflection /
   Handoff / Write Gateway / ToolEngine（约 1600 行）/ console_trace（约 1600 行），
   外加 38 工具的 ToolSpec 注册表与 20 个 Worker 私有工具合同。
2. **LangGraph 多 Agent 编排**（存在约一个月，`dd5b6c7` 删除）：supervisor 节点
   （decompose → plan → Send fan-out → collect → synthesize）+ 9 个
   `create_agent` Worker 子图 + 失败重派，本质是把自研编排换了实现。
3. **扁平领域工具直连 + 专家子循环**（`bbe668e`）：仍保留 3 个专家封装循环。

## 决策

- **一个主 Agent**（`langchain.agents.create_agent`），编译后的图直接作为节点挂入
  父图（`START → agent → END`）。
- **约 15 个领域只读工具直接挂载**：证据检索、内部数据（预测/排名/实体解析/组合/
  账户/画像）、金融图关系，风险分析为确定性工具（`provider.analyze_risk`，不经 LLM）。
  不再有"专家子代理"——每个能力不再有自己的 LLM 循环。
- **提案与写操作是工具**：`preview_*` 生成待审批 Proposal；`confirm_pending_proposal`
  与图写入经 LangGraph `interrupt()` 审批。
- **编译后的主 Agent 图作为节点**使工具内 `interrupt()` 经父 checkpointer
  （Postgres，thread_id=会话）传播到顶层；pending-actions 的 confirm/reject 以
  `Command(resume=...)` 在**被中断的工具内部**继续执行，无重放。
- **无会话记忆复用**：SessionStateStore 会话摘要注入、长期记忆 PostgresStore、
  memory_* 表全部删除。checkpointer 仅服务审批中断恢复。
- **MCP stdio 链保留**：主 Agent 读工具的数据面仍经
  `worker_tools → invoke_worker_mcp → data/rag/model server`（每次调用 spawn
  子进程）。这是当前已知最大的架构遗留成本，见"后果"。

## 理由（真实权衡）

- **两阶段 DAG 规划 vs 单循环自主工具选择**：前者可控性强（最小工具面、槽位绑定），
  但需要大量自研编排代码且不可观测性差；后者代码量约为前者 1/5，依赖现代模型的
  工具选择能力。项目自用、无外部用户 → 选简单。
- **专家子循环 vs 直连工具**：专家循环保住"最小工具面"，但多一层 LLM 调用
  （延迟/费用）且工具间引用传递（GraphRef dict）更绕。直连后权威 ID 纪律由
  system prompt 承担。
- **内层子图作为节点 vs 函数节点内 invoke**：前者让中断/恢复由 LangGraph 原生
  处理；后者需要手工传播 `GraphInterrupt` 且恢复语义不可靠（实测踩坑）。
- **interrupt 审批 vs 旧确认令牌 RPC**：旧流程（pending plan + token + 双路由）
  保留在提交网关层（`writes/gateway.py`），但 Agent 内的审批统一走 interrupt。

## 后果

- 正面：`agent_graph/graph.py` 约 100 行；工具链一层直达；审批语义由框架保证。
- 负面/已知成本：
  - `internal.*`/`evidence.*` 读工具仍走 stdio MCP 子进程（每调用 spawn），
    这是自研时代的运输层遗留；若要精简，下一步是把三个 server 的 adapter
    函数化直调，删除 `mcp/` 运输层（未做，属于独立的可逆决策）。
  - `workers/cards.py` 只剩 4 个角色常量（工具归属元数据），不再描述任何行为。
  - 前端运行详情页（`/runs/{run_id}`）数据源收敛为
    `agent_runs/agent_steps/agent_tool_calls/agent_sources` +
    `action_proposals/approvals/commits`；其余 8 张遗留表已出 drop 迁移
    （`database/postgres_migrations/003_drop_legacy_agent_tables.sql`，未执行）。
