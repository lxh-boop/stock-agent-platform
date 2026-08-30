# PostgreSQL Persistence

项目业务数据与 Agent Runtime（智能体运行时）统一使用 PostgreSQL。

- `stock_app`：业务、Agent、用户、行情、预测、组合、RAG 元数据。
- `stock_runtime`：Task Runtime（任务运行时）、Checkpoint（检查点）、Session State（会话状态）、Memory（运行时记忆）。
- `database.postgres_store.PostgresStore`：唯一 CRUD（增删改查）边界。
- PostgreSQL 是唯一 `Source of Truth（事实源）` 和唯一正式读写路径。

Stage 3.6B-8 安装完成后，旧文件数据库只存在于 `D:\google\postgres_final_backup` 的离线恢复备份中，不参与任何运行时链路。
