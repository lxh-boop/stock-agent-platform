-- 003_drop_legacy_agent_tables.sql
-- 删除自研 Agent 运行时的遗留表。这些表自 2026-11 破坏式重构后无任何写入方
-- 与读取方（会话记忆复用、长期记忆、RunCheckpointStore、turn 摘要均已删除）：
--   memory_items / memory_links / memory_records
--   agent_session_state_items / agent_session_state_access_log
--   agent_run_checkpoints / agent_request_checkpoints
--   agent_turn_summaries
-- 保留：agent_runs / agent_steps / agent_tool_calls / agent_sources（新引擎写入）、
--       action_proposals / action_approvals / action_commits（写执行链）、
--       conversations / messages / proposals 系（产品功能）。
-- 幂等：全部使用 IF EXISTS，可重复执行。

-- stock_app schema
DROP TABLE IF EXISTS stock_app.memory_links;
DROP TABLE IF EXISTS stock_app.memory_items;
DROP TABLE IF EXISTS stock_app.agent_turn_summaries;
DROP INDEX IF EXISTS stock_app.idx_memory_records_user_type_status;
DROP INDEX IF EXISTS stock_app.idx_memory_records_source;
DROP TABLE IF EXISTS stock_app.memory_records;
DROP INDEX IF EXISTS stock_app.idx_agent_session_state_lookup;
DROP INDEX IF EXISTS stock_app.idx_agent_session_state_expiry;
DROP INDEX IF EXISTS stock_app.idx_agent_session_state_access_task;
DROP TABLE IF EXISTS stock_app.agent_session_state_access_log;
DROP TABLE IF EXISTS stock_app.agent_session_state_items;

-- stock_runtime schema
DROP TABLE IF EXISTS stock_runtime.agent_run_checkpoints;
DROP TABLE IF EXISTS stock_runtime.agent_request_checkpoints;
