-- Account-owned LLM/Tushare credentials share the existing user_account table.
-- Safe to run more than once; existing passwords and sessions are preserved.
ALTER TABLE stock_app.user_account
    ADD COLUMN IF NOT EXISTS llm_config_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE stock_app.user_account
    ADD COLUMN IF NOT EXISTS llm_api_key_ciphertext TEXT NOT NULL DEFAULT '';
ALTER TABLE stock_app.user_account
    ADD COLUMN IF NOT EXISTS tushare_token_ciphertext TEXT NOT NULL DEFAULT '';
