# Current Requirements

本文档是当前需求的接手口径。若旧文档、旧页面文案或旧示例与本文件冲突，以本文件和标注的实现文件为准。

## 项目定位

本项目是面向 A 股研究场景的 React + FastAPI 多 Agent 金融分析工作台，覆盖数据更新、模型排名、新闻证据、历史回测、AI 模拟盘和可控工具运行时。

系统只用于机器学习、金融数据分析和项目展示，不接券商接口，不构成投资建议，不用于实盘交易。

所有用户可见页面和 Agent 输出必须保留免责声明：

```text
本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。
```

## 当前架构

```text
React 19 / TypeScript / Vite / Ant Design
  -> FastAPI / Pydantic Presenter
  -> Application Service
  -> Task Runtime / Agent Collaboration
  -> Worker DAG / ToolExecutor / MCP Runtime
  -> PostgreSQL / Neo4j / 宿主机模型与数据
```

旧 Streamlit、`app.py`、`app/**` 和 `client/api/**` 已下线，不再是正式入口。

## 当前有效能力

- 预测排名：读取 PostgreSQL / `outputs/ranking_latest.csv` 的模型排名，支持 TopK、图表与个股信息。
- 主动排名模型：`cross_sectional_ranker`，版本 `2026.08.29.1`，后端 `registered_ranker`，两个 LightGBM 成员做日截面百分位秩融合。
- 数据更新：`daily_incremental_update.py` 负责行情、Tushare 特征、主动排名生成；15:30 前自动使用上一已完成交易日。
- 模拟盘：仅 paper trading；策略固定为 `hierarchical_top10`，所有写路径必须经过 Proposal / Approval / 服务端复校。
- Agent：React 页面通过 `/api/v1/tasks` 提交 `agent.run`，通过 SSE 获取事件；服务端负责最终回答持久化。
- 非业务问题：`RequestCategory.GENERAL` 可直接由 LLM 回答，不进入 Need / Worker / Tool DAG。

## 关键实现入口

| 领域 | 入口 |
| --- | --- |
| API envelope / contract | `server/api/contracts.py`、`contracts/stage6/` |
| 任务提交、SSE、恢复 | `server/api/tasks.py`、`server/task_runtime/` |
| Agent 单入口 | `agent/executor.py`、`agent/collaboration/integration.py` |
| Request / Need / Worker 分层 | `agent/collaboration/request_bundle.py`、`agent/collaboration/planner.py` |
| Worker 私有 Tool DAG | `agent/tool_dag/`、`agent/tool_runtime/`、`agent/worker_tools/` |
| 主动排名模型 | `daily_incremental_update.py`、`ranking_runtime/`、`stock_ranker/` |
| 设置与密钥 | `application/web_settings_service.py`、`core/config/secrets.py` |

## Provider 与模型配置

- LLM Provider 支持显式注册；`zai` / `zhipu` / `glm` 使用 OpenAI-compatible adapter。
- ZAI 的 `glm-5.3-flash` 走 Responses API，不走 Chat Completions。
- 每个任务绑定提交时的 `profile_id`、`config_hash`、provider 和 model，避免运行中配置漂移。
- LLM API Key 只能保存在服务端 secret store、环境变量或受控文件中，禁止返回浏览器或写入 Git。

## 数据与调度口径

- 业务运行数据以 PostgreSQL 为权威；文件和 CSV 只作为缓存、产物或导出。
- Neo4j 只承载 GraphRef / 金融实体权威关系，不替代业务数据库。
- 每日调度在 15:30 前自动解析为上一已完成交易日，15:30 后才解析为当日。
- 调度器内部失败必须向 Task Runtime 抛出异常；外层任务不得在业务失败时显示 `succeeded`。

## Agent 分层边界

Main Agent / Request Planner 只能看到：

- 规范化用户请求；
- 会话摘要和少量上下文；
- 压缩版 Worker capability catalog；
- Need / Worker binding 合同。

Main Agent 不能看到：

- 私有 Tool ID、Tool schema 和 Tool 参数；
- 数据库连接串、服务器路径；
- LLM/Tushare/Neo4j 凭据；
- 原始 Tool payload。

Worker Tool Planner 只能看到被选中 Worker 的私有 Tool 目录，并输出可验证 Tool DAG。

## 权限与合规边界

AI 只能做研究、解释、证据检索、排名、组合状态读取和 paper trading 计划生成。

禁止：

- 生成实盘买卖指令；
- 连接券商或自动下单；
- 绕过 service / repository 直接改业务数据；
- 把新闻相似度或情绪直接当作收益预测；
- 泄露 Token、API Key、密码或确认令牌。
