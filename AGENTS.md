# A股每日股票评分系统接手说明

## 项目定位

本项目是面向金融分析和模拟盘验证的多 Agent 工程。系统不连接真实交易，页面和文档必须保留：

```text
本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。
```

## Stage 6.5 正式运行架构

```text
浏览器
  -> React / Nginx（http://127.0.0.1:3000）
  -> FastAPI（http://127.0.0.1:8010）
  -> Application Service
  -> Task Runtime / Agent / RAG / Portfolio
```

Streamlit、`app.py`、`app/**` 和旧 `client/api/**` 已下线。不得重新引入浏览器直读数据库、文件、模型或密钥的实现。

## 开发环境

当前机器示例：

```text
项目目录：D:\cht\agent\stock-agent-platform
Python：D:\cht\agent\stock-agent-platform\.venv\Scripts\python.exe
```

不要使用 `py -3`、裸 `python` 或 C 盘解释器执行项目测试和维护脚本。

## 生产启动

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\docker\start_compose.ps1
```

或使用安装到 `D:\google` 的一键脚本：

```text
D:\google\D_google_stage_06_5_build_and_start.bat
```

正式服务只有：

```text
frontend  3000
api       8010
```

本地脱离 Docker 调试时，可在 `frontend/` 执行：

```powershell
npm run dev
```

默认地址：

```text
frontend  5173
api       8010
```

## 本地服务启动（脱离 Docker 调试）

```powershell
# PostgreSQL（便携版，127.0.0.1:5432）
powershell -ExecutionPolicy Bypass -File D:\cht\agent\postgres\start_pg.ps1
# Neo4j（便携版 Community 5.26 + JDK21，127.0.0.1:7687；密码在 D:\cht\agent\neo4j\secrets\neo4j_password.txt）
powershell -ExecutionPolicy Bypass -File D:\cht\agent\neo4j\start_neo4j.ps1
```

API 进程需要同时注入 `STOCK_POSTGRES_*`（见 `D:\cht\agent\postgres\dev_env.ps1`）和
`NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD` 环境变量，缺任一组对应 readiness 检查会失败。
金融图首次初始化：`scripts/init_financial_graph.py`，证券主数据导入：
`scripts/import_security_master.py datacsi300_stock_pool.csv`。

## 分层约束

```text
React -> FastAPI Router/Schema/Presenter -> Application Service -> Domain/Runtime
```

- React 只调用 `/api/v1/**`；
- 长任务通过 Task API 和 SSE；
- 浏览器只能保存会话 ID、task_id 和 last_event_id 等恢复标识；
- 写操作必须二次确认、服务端复校和幂等提交；
- `application/support/**` 只能放置 UI 无关的共享读取和格式化能力；
- 不允许从 `application/**` 或 `agent/**` 反向依赖前端目录。
- 业务运行数据以 PostgreSQL 为权威；Neo4j 只承载金融图权威关系。
- LLM Provider 必须经过 registry 和 adapter；不同 Provider 的协议差异不得散落到 Planner。

## 主要目录

```text
frontend/             React 正式前端
server/api/           FastAPI 路由、DTO 和 Presenter
server/task_runtime/  长任务状态、执行和 SSE
application/          应用服务与 support helper
agent/                Main/Worker Agent、DAG、工具与安全写链路
portfolio/            模拟盘与组合逻辑
rag/                  混合检索
contracts/stage6/     冻结合同
scripts/docker/       生产启动与验收
scripts/refactor/     架构、合同和浏览器检查
```

## 验收

```text
D:\google\D_google_test_stage_06_5.bat
```

验收必须覆盖：合同检查、阶段 6.1～6.5 架构检查、核心单测、Task Runtime、真实 Chrome、刷新恢复、页面路由、同源 API/SSE 和敏感信息检查。

## 数据与敏感信息

`data/`、`models/`、`outputs/`、`logs/`、`runtime/`、`external_repos/` 和 `local_app_config.json` 由宿主机挂载，不得复制进镜像或交付包。Token、API Key、密码和确认令牌不得进入 React、日志或 Git。
