# Project Status

更新时间：2026-09-23

## 当前可用状态

- 正式运行时：React 19 + FastAPI + PostgreSQL + Neo4j。
- 本地开发前端：`http://127.0.0.1:5173`。
- 本地 API：`http://127.0.0.1:8010`。
- Streamlit、`app.py`、`app/**` 和旧 `client/api/**` 已下线。
- 主动排名模型：`cross_sectional_ranker`，版本 `2026.08.29.1`。
- 模型后端：`registered_ranker`，使用两个 LightGBM 成员做日截面百分位秩融合。
- AI Agent：单一入口 `agent.run`，任务通过 Task API 提交并通过 SSE 恢复。
- 模拟盘：策略固定为 `hierarchical_top10`，写操作仍走 Proposal / Approval / Execute。

## 2026-09-23 更新

### Agent 模型与路由

- ZAI / Zhipu / GLM Provider 已注册到 OpenAI-compatible adapter。
- `glm-5.3-flash` 通过 Responses API 调用，不再误报 Provider 未注册。
- 新增 `RequestCategory.GENERAL`；无业务数据的问候、身份和能力问题可直接回答，不进入 Need / Worker / Tool DAG。
- Need / Worker Planning 使用压缩版 Worker capability catalog，不再读取私有 Tool 明细。

### 运行可观测性

- Agent Flow 事件记录 `run_elapsed_ms` 和 `since_previous_ms`。
- 运行结束生成性能耗时摘要，包含 LLM、Tool 和最慢阶段。
- Task Worker 成功 / 失败事件记录 `total_seconds`。

### 数据库与任务语义

- 业务运行数据以 PostgreSQL 为权威。
- Neo4j schema 在同一进程内按连接配置缓存，避免每次请求重复初始化。
- 调度器业务失败会向 Task Runtime 抛出明确异常；外层任务不再把业务失败显示为 `succeeded`。
- 任务失败信息包含失败步骤、错误消息和 traceback tail。

### Skill Runtime（2026-09-23 下午）

- 新增 `agent/skill_runtime/`：Agent Skills 开放标准（SKILL.md + YAML frontmatter）的加载、校验、安全扫描与注册。
- 权威存储为文件系统：内置 skill 在仓库 `skills/`；用户 skill 在 `runtime/skills/<user_id>/<skill_name>/`（宿主机挂载、不进镜像/Git），准入状态持久化在目录内 `skill.meta.json`。
- 准入状态机 admitted / disabled / rejected：安全扫描（密钥样式）rejected 永远优先于用户开关；`scripts/` 只记录清单、永不执行。
- 新增 API `server/api/routers/web_skills.py`（/api/v1/web/skills：列表/创建/上传/GitHub 导入/启停/删除，仅本人可见）；GitHub 仅公开仓库，codeload zip 拉取 + zip-slip 防护。
- Worker 注入：W04/W05/W09 的 system prompt 追加 admitted skill 正文，平台约束块永远拼在 skill 内容之后；skill manifest（name/version/content_hash）经 `llm_audit.note_applied_skills` 进入 LLM_CALL 审计事件 `applied_skills` 字段。
- 前端新增"技能管理"页（/skills）：新建、GitHub 导入、zip 上传、详情、启停、删除。
- 首个内置 skill：`skills/compliance_risk_control`（v1.0.0，合规与风险边界检查）。

## 当前验证

- skill_runtime / web_skills 单测 23 项通过（含用户隔离、rejected 优先、zip-slip、约束后置断言）。
- 前端 `tsc --noEmit` 通过。
- 存量失败（非本次引入，2026-09-23 上午清理/重构遗留）：`test_llm_io_optimization_v183_safe` 因 `agent.collaboration.agent_directory` 缺失无法收集；`test_local_llm_runtime` 11 项失败，另有多组 Worker 测试仍引用旧签名 `working_memory_context`。
- ZAI Responses API adapter 单测通过。
- 调度日期与调度 worker 回归通过。
- 设置页错误透传相关测试通过。
- API health、前端页面和前端代理检查通过。

## 待处理

- 继续压缩 Need / Worker Planner 和 Worker Tool Planner 的 Prompt 输入。
- 为常见稳定业务请求增加确定性计划模板。
- 在进入 Worker Tool DAG 前增加更强的数据就绪预检。
- 清理历史 Streamlit 文档口径，避免与当前 React / FastAPI 架构混淆。
