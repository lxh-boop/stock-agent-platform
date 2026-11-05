# ADR 0002：删除内部 MCP 运输层；审批采用官方 HITL 中间件；不接外部 MCP

- 状态：已接受（2026-11）
- 前置：ADR 0001（单主 Agent 扁平直连）

## 背景

ADR 0001 之后仍残留三块自研 agent 框架件：

1. **内部 MCP 运输层**（`agent_graph/mcp/`，1823 行）：13 个领域读工具里有 7 个
   的真实数据面是 `handler → invoke_worker_mcp → 鉴权 → spawn stdio 子进程
   （python -m ..._server）→ MCP 协议 → adapter → 调用自己的 services 函数`。
   自建 MCP server 包装的是**同项目内的函数**，每次工具调用 spawn 子进程，
   每次构建注册表还会 spawn 3 个子进程做 discovery（顺带发现
   `data_server` 的 main() 读不存在的 `args.db_path`，单独启动必炸——随删除消亡）。
2. **工具内自研审批分支**：写工具在函数体内调 `interrupt()`，审批语义混在业务代码里。
3. **dispatch 工具注册表**（456 行）：`execute_tool` 的 51→10 个定义只是给
   提交网关和两个 REST 预览当间接层，网关本来就是 intent→adapter 的一张表。

## 决策

- **删内部 MCP 运输层**：7 个 MCP-mediated handler 改为直调
  `evidence_service` / `model_inference_service` / `data_query_service`
  （返回形状逐字段对齐，含 Data adapter 的 `data.data` 解包语义以
  `_data_envelope` 复刻）；`mcp/` 全包、worker_tools 注册表的投影过滤、
  requirements 里的 `mcp>=2,<3` 一并移除。工具对 LLM 的名字/参数不变。
- **审批采用官方 `HumanInTheLoopMiddleware`**（`interrupt_mode="per_call"`，
  `allowed_decisions=["approve","reject"]`）：写工具
  （`confirm_pending_proposal`、两个图写入）函数体回到纯业务；
  中断经父 checkpointer 传播，pending-actions 的 confirm/reject 由
  `resume_langgraph_agent` 换算成按 interrupt id 的
  `{"type": "approve"|"reject", "message": str}` 应答，在被中断处继续执行。
- **dispatch 扁平化**：删 `dispatch.py`；提交网关改为
  `intent → adapter 函数` 直调（`PLAN_INTENT_TO_ADAPTER`），两个 REST 预览
  直调同一批 adapter 函数；结果统一经 `normalise_raw_result` 归一为 dict
  （REST 的 `_result_payload` 本就兼容 dict）。
- **不接外部 MCP**（评估后拒绝）：联网搜索/实时行情/浏览器 MCP 都会把
  未审核的外部内容或盘中价格灌进按"策划 RAG 管道 + T+1 日线"设计的系统，
  破坏证据的可控性与合规边界。若未来盘中时效成为真实需求，
  `langchain-mcp-adapters` + 允许清单是标准接入点，属可逆决策。

## 理由（真实权衡）

- **MCP 的适用边界**：MCP 用于连接**外部**服务；对同进程函数套 MCP 协议
  是纯运输开销（延迟 + 进程管理 + 双份 schema 校验），langchain 正统形态
  是"工具=函数"。
- **中间件 vs 工具内 interrupt**：两者都是 LangGraph 原生；中间件把
  "哪些工具需要审批"变成 agent 构建处的声明式配置，工具体保持纯净，
  reject 的合成 ToolMessage 语义由框架保证（模型会收到"未执行、勿重试"）。

## 后果

- 正面：每次 run 少 spawn 3 个 discovery 子进程 + 每次读工具调用少 spawn
  1 个子进程；`data_server` 启动缺陷随删除消亡；写工具体纯净。
- 负面/边界：
  - 外部 MCP 接入点被明确**拒绝**（非延迟）——需要新证据源时应走既有
    "爬取→入库→RAG"管道，而不是临时接 MCP。
  - `mcp` SDK 从依赖中移除；若将来接外部 stdio MCP server，恢复
    `langchain-mcp-adapters`（自带 transport，不需要旧 SDK 常驻）。
  - 前端/REST 契约不变：`/pending-actions` 提交流程、`lg_approve_{run_id}`
    plan_id、`CONFIRM-xxxxxx` 短语全部照旧。
