# 项目记忆

## 项目概览
- **名称**：A股每日股票评分系统（StockDailyApp v0.1.0）
- **定位**：面向金融分析和模拟盘验证的多 Agent 工程。仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。
- **架构阶段**：Stage 6.5，已落地 React + FastAPI + Docker Compose 正式运行架构。

## 运行架构
```
浏览器 -> React/Nginx(:3000) -> FastAPI(:8010) -> Application Service -> Task Runtime / Agent / RAG / Portfolio
```
- 前端：React 19 + Ant Design 6 + Vite 8 + Zustand + TanStack Query + React Router 7
- 后端：FastAPI（API v4.0.0），Router/Schema/Presenter 三层分离
- Task Runtime：长任务状态持久化、SSE 推送、中断恢复
- Agent：多智能体协作（Supervisor / Market Intelligence / Portfolio Analysis / Reporting / Risk Operation）
- MCP：Data / RAG / Model 三类内部能力通过官方 MCP SDK (stdio) 暴露

## 分层约束
- React 只调用 `/api/v1/**`；长任务通过 Task API + SSE
- 写操作必须二次确认（Proposal → Approval → Execute 安全链路）
- 浏览器只能保存会话 ID、task_id、last_event_id
- 不允许 application/ 或 agent/ 反向依赖前端

## 关键目录
- `frontend/` — React 正式前端
- `server/api/` — FastAPI 路由、DTO、Presenter
- `server/task_runtime/` — 长任务管理（Manager/Store/Worker/Handlers）
- `application/` — 应用服务（Agent/Dashboard/Paper/Profile/Web 等）
- `agent/` — 多智能体（Executor/Collaboration/Graph/Handoff/MCP/Memory/Context/Capabilities）
- `portfolio/` — 模拟盘引擎（账户/持仓/现金流/风险/决策归因）
- `rag/` — 混合检索（BM25 + Dense + Reranker）
- `scheduler/` — 每日定时任务（RuntimeScheduler + DailyWorker + 交易日历）
- `contracts/stage6/` — 冻结 API 合同（JSON Schema）
- `scripts/docker/` — 生产启动与验收脚本

## 开发环境
- Python: `D:\stock_daily_app\.venv\Scripts\python.exe`
- 生产启动: `D:\google\D_google_stage_06_5_build_and_start.bat`
- 验收: `D:\google\D_google_test_stage_06_5.bat`
- Git 分支: main（活跃）

## 外部依赖
- 数据：Qlib（D:\qlib_data\cn_data）、Tushare、AKShare
- 模型：Kronos-mini（默认）、DFT-UNet（外部模型）、Ollama（本地 LLM）
- 图数据库：Neo4j（金融事实图）
- LLM：OpenAI 兼容 API / Ollama 本地

## 编码规范
- 所有英文命名（变量名、函数名、类名、常量等）必须附带中文注释说明其含义
- 适用于 Python、TypeScript/TSX、配置文件等所有代码文件

## 结构清单输出规范（用户强约束，每次执行）
- 当用户要求「结构清单 / 字段清单」时，一律使用扁平模板：
  ```
  类名
  字段名 → 中文说明（一句话解释该字段含义/作用）
  ```
- 嵌套对象（如 context_binding）用缩进展开子字段
- 所有英文变量名必须带中文注释，同一条指令内同时生效，不可省略
- 示例参考：docs/context_contract.md

## 前端页面路由
- `/dashboard` — 股票评分排名
- `/stocks/:code` — 个股详情
- `/models/metrics` — 模型指标
- `/models/search` — 模型搜索
- `/backtests` — 回测
- `/news` — 新闻
- `/paper-trading` — 模拟盘
- `/agent` — Agent 交互
- `/monitor` — 系统监控
- `/settings` — 设置
