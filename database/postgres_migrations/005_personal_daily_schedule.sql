-- 每个账号自己的日更计划；不更新角色，不移动已有持仓或密码。
ALTER TABLE stock_app.user_account
    ADD COLUMN IF NOT EXISTS scheduler_config_json TEXT NOT NULL DEFAULT '{}';
-- 个人行情/排名/新闻数据域在首次使用时按服务端认证身份幂等创建。
-- 它们仍在同一个 PostgreSQL 数据库，数据库整库备份会包含这些 schema。
