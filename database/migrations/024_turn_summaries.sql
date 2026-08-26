-- 轮次摘要表：ConversationInventory 的数据源，支撑回答级复用
-- 每轮 agent 回答落库后写入一条，供 decompose 粗筛与规则层保护校验使用
CREATE TABLE IF NOT EXISTS agent_turn_summaries (
    turn_id TEXT PRIMARY KEY,              -- 轮次编号（真实 ID，prompt 中用显示短编号映射）
    user_id TEXT NOT NULL,                 -- 用户 ID（复用作用域：同用户可跨会话复用）
    conversation_id TEXT,                  -- 来源会话 ID（元数据，不做硬过滤）
    run_id TEXT,                           -- 关联的执行 run 编号
    trade_date TEXT,                       -- 所属交易日（YYYY-MM-DD，非交易日可为空）
    user_summary TEXT,                     -- 用户提问摘要（一行，LLM 判断面）
    assistant_summary TEXT,                -- 助手回答摘要（一行，LLM 判断面）
    artifact_refs_json TEXT,               -- 该轮产出 artifact 引用名单（JSON 数组，数据级保护凭据）
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- 生成时间（轮次级 TTL 保护依据）
    metadata_json TEXT                     -- 扩展元数据（JSON 对象）
);

CREATE INDEX IF NOT EXISTS idx_turn_summaries_user_created
ON agent_turn_summaries(user_id, created_at);
