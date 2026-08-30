--
-- PostgreSQL database dump
--

\restrict YVmZq1DTMXFjl4P9lvGxyAnCozt2t7h2OWiA4H62fXINDtIqN0eKndxJqLOZH9e

-- Dumped from database version 16.15
-- Dumped by pg_dump version 16.15

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: stock_app; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA stock_app;


--
-- Name: stock_runtime; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA stock_runtime;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: action_approvals; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.action_approvals (
    approval_id text NOT NULL,
    plan_id text NOT NULL,
    user_id text NOT NULL,
    plan_hash text NOT NULL,
    snapshot_id text,
    business_state_version text,
    status text NOT NULL,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    used_at text,
    expires_at text,
    metadata_json text
);


--
-- Name: action_commits; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.action_commits (
    commit_id text NOT NULL,
    plan_id text NOT NULL,
    approval_id text,
    user_id text NOT NULL,
    status text NOT NULL,
    idempotency_key text,
    before_state_hash text,
    after_state_hash text,
    result_summary_json text,
    error_type text,
    error_message text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    committed_at text,
    metadata_json text
);


--
-- Name: action_proposals; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.action_proposals (
    plan_id text NOT NULL,
    user_id text NOT NULL,
    run_id text,
    operation_type text NOT NULL,
    snapshot_id text,
    business_state_version text,
    plan_hash text NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    expires_at text,
    before_state_summary_json text,
    proposed_changes_json text,
    after_state_preview_json text,
    warnings_json text,
    validation_results_json text,
    requires_confirmation bigint DEFAULT 1 NOT NULL,
    metadata_json text
);


--
-- Name: agent_action_log; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_action_log (
    action_id text NOT NULL,
    session_id text,
    user_id text NOT NULL,
    intent text,
    tool_name text,
    tool_input text,
    tool_output_summary text,
    plan_id text,
    confirmation_status text,
    execution_status text,
    decision_source text,
    trade_date text,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    confirmed_at text,
    executed_at text,
    error_message text
);


--
-- Name: agent_confirmation_log; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_confirmation_log (
    confirmation_id text NOT NULL,
    session_id text,
    user_id text NOT NULL,
    plan_id text NOT NULL,
    confirmation_token_hash text,
    confirmation_status text,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    confirmed_at text,
    expires_at text,
    error_message text
);


--
-- Name: agent_decision_log; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_decision_log (
    decision_id text NOT NULL,
    user_id text,
    trade_date text,
    stock_code text,
    original_pred_score double precision,
    original_pred_rank bigint,
    news_adjustment text,
    risk_adjustment text,
    user_constraint text,
    triggered_rules text,
    combined_adjustment double precision,
    position_adjustment_ratio double precision,
    final_reason text,
    evidence_news_ids text,
    evidence_chunk_ids text,
    evidence_snapshot text,
    retrieval_id text,
    future_return_1d double precision,
    future_return_5d double precision,
    is_effective bigint,
    job_id text,
    run_id text,
    execution_source text,
    created_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: agent_rule; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_rule (
    rule_id text NOT NULL,
    rule_name text,
    rule_type text,
    condition text,
    action text,
    priority bigint,
    is_active bigint DEFAULT 1,
    description text,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    updated_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: agent_runs; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_runs (
    run_id text NOT NULL,
    conversation_id text,
    user_id text NOT NULL,
    goal text NOT NULL,
    status text NOT NULL,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    started_at text,
    finished_at text,
    error_type text,
    error_message text,
    metadata_json text
);


--
-- Name: agent_sandbox_runs; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_sandbox_runs (
    sandbox_run_id text NOT NULL,
    run_id text,
    step_id text,
    user_id text NOT NULL,
    snapshot_id text,
    code_hash text NOT NULL,
    status text NOT NULL,
    stdout_summary text,
    result_summary_json text,
    generated_files_json text,
    refusal_reason text,
    error_type text,
    error_message text,
    started_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    finished_at text,
    duration_seconds double precision DEFAULT 0,
    metadata_json text
);


--
-- Name: agent_sources; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_sources (
    source_id text NOT NULL,
    run_id text,
    message_id text,
    tool_call_id text,
    user_id text NOT NULL,
    source_type text NOT NULL,
    source_title text,
    source_time text,
    database_record_id text,
    file_path text,
    content_hash text,
    retrieved_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    snippet text,
    metadata_json text
);


--
-- Name: agent_steps; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_steps (
    step_record_id text,
    step_id text NOT NULL,
    run_id text NOT NULL,
    parent_step_id text,
    intent text,
    status text NOT NULL,
    depends_on_json text,
    tool_name text,
    tool_args_summary_json text,
    observation_summary text,
    error_type text,
    error_message text,
    retry_count bigint DEFAULT 0,
    started_at text,
    finished_at text,
    duration_seconds double precision DEFAULT 0,
    metadata_json text
);


--
-- Name: agent_tool_call_log; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_tool_call_log (
    call_id text NOT NULL,
    session_id text,
    user_id text NOT NULL,
    tool_name text NOT NULL,
    tool_input text,
    tool_output_summary text,
    status text,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    error_message text
);


--
-- Name: agent_tool_calls; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_tool_calls (
    tool_call_id text NOT NULL,
    run_id text,
    step_id text,
    user_id text NOT NULL,
    tool_name text NOT NULL,
    status text NOT NULL,
    input_summary_json text,
    output_summary_json text,
    error_type text,
    error_message text,
    started_at text,
    finished_at text,
    duration_seconds double precision DEFAULT 0,
    retry_count bigint DEFAULT 0,
    metadata_json text
);


--
-- Name: agent_turn_summaries; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.agent_turn_summaries (
    turn_id text NOT NULL,
    user_id text NOT NULL,
    conversation_id text,
    run_id text,
    trade_date text,
    user_summary text,
    assistant_summary text,
    artifact_refs_json text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    metadata_json text
);


--
-- Name: artifacts; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.artifacts (
    artifact_id text NOT NULL,
    user_id text NOT NULL,
    run_id text,
    artifact_type text NOT NULL,
    path text NOT NULL,
    content_hash text,
    size_bytes bigint DEFAULT 0,
    retention_policy text DEFAULT 'standard'::text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    expires_at text,
    metadata_json text
);


--
-- Name: backtest_evaluation; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.backtest_evaluation (
    eval_id text NOT NULL,
    strategy_name text,
    start_date text,
    end_date text,
    topk bigint,
    buffer bigint,
    annual_return double precision,
    information_ratio double precision,
    sharpe_ratio double precision,
    max_drawdown double precision,
    turnover double precision,
    win_rate double precision,
    agent_modify_count bigint,
    useful_modify_count bigint,
    false_modify_count bigint,
    missed_risk_count bigint,
    created_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: conversation_summaries; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.conversation_summaries (
    summary_id text NOT NULL,
    conversation_id text NOT NULL,
    user_id text NOT NULL,
    summary_text text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    covered_message_count bigint DEFAULT 0,
    token_estimate bigint DEFAULT 0,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    metadata_json text
);


--
-- Name: conversations; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.conversations (
    conversation_id text NOT NULL,
    user_id text NOT NULL,
    title text,
    status text DEFAULT 'active'::text NOT NULL,
    language text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    last_message_at text,
    metadata_json text
);


--
-- Name: industry_event_rule; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.industry_event_rule (
    rule_id text NOT NULL,
    event_keyword text,
    affected_industry text,
    relation_type text,
    impact_direction text,
    base_strength double precision,
    description text,
    updated_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: investment_goal; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.investment_goal (
    goal_id text NOT NULL,
    user_id text NOT NULL,
    goal_type text,
    target_return double precision,
    target_period text,
    priority text,
    capital_usage text,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    updated_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: market_data_daily; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.market_data_daily (
    trade_date text NOT NULL,
    stock_code text NOT NULL,
    open double precision,
    high double precision,
    low double precision,
    close double precision,
    volume double precision,
    amount double precision,
    return_1d double precision,
    volatility_20d double precision,
    turnover_rate double precision,
    updated_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: memory_items; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.memory_items (
    memory_id text NOT NULL,
    user_id text NOT NULL,
    conversation_id text,
    memory_type text NOT NULL,
    content text NOT NULL,
    topics_json text,
    stock_codes_json text,
    company_names_json text,
    industries_json text,
    importance double precision DEFAULT 0,
    status text DEFAULT 'active'::text NOT NULL,
    source_type text,
    source_id text,
    valid_from text,
    valid_until text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    supersedes_memory_id text,
    metadata_json text
);


--
-- Name: memory_links; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.memory_links (
    link_id text NOT NULL,
    memory_id text NOT NULL,
    linked_type text NOT NULL,
    linked_id text NOT NULL,
    relation text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    metadata_json text
);


--
-- Name: messages; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.messages (
    message_id text NOT NULL,
    conversation_id text NOT NULL,
    user_id text NOT NULL,
    role text NOT NULL,
    content text NOT NULL,
    language text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    token_estimate bigint DEFAULT 0,
    metadata_json text
);


--
-- Name: model_prediction; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.model_prediction (
    prediction_id text NOT NULL,
    trade_date text NOT NULL,
    stock_code text NOT NULL,
    model_name text NOT NULL,
    pred_score double precision,
    pred_rank bigint,
    pred_return double precision,
    risk_score double precision,
    confidence text,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    prediction_for_date text,
    stock_name text,
    risk_level text,
    source_kind text DEFAULT 'ranking'::text NOT NULL,
    payload_json text DEFAULT '{}'::text NOT NULL,
    updated_at text
);


--
-- Name: news_chunk; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.news_chunk (
    chunk_id text NOT NULL,
    news_id text NOT NULL,
    chunk_index bigint,
    chunk_text text NOT NULL,
    section_title text,
    source text,
    publish_time text,
    trade_date text,
    stock_code text,
    industry text,
    event_type text,
    is_announcement bigint DEFAULT 0,
    used_in_decision bigint DEFAULT 0,
    retrieval_count bigint DEFAULT 0,
    importance_score double precision,
    retention_level text DEFAULT 'hot'::text,
    expire_at text,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    decision_id text,
    content_level text DEFAULT 'title_only'::text,
    title text,
    stock_codes_json text,
    entities_json text,
    metadata_json text
);


--
-- Name: news_embedding; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.news_embedding (
    embedding_id text NOT NULL,
    chunk_id text NOT NULL,
    embedding_model text,
    embedding_dim bigint,
    embedding_path text,
    embedding bytea,
    created_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: news_event; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.news_event (
    news_id text NOT NULL,
    title text,
    summary text,
    content text,
    raw_file_path text,
    archive_file_path text,
    source text,
    publish_time text,
    trade_date text,
    event_type text,
    sentiment text,
    importance_score double precision,
    is_announcement bigint DEFAULT 0,
    url text,
    content_hash text,
    retention_level text DEFAULT 'hot'::text,
    is_major_event bigint DEFAULT 0,
    is_used_by_agent bigint DEFAULT 0,
    raw_content_saved bigint DEFAULT 0,
    expire_at text,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    content_level text DEFAULT 'title_only'::text
);


--
-- Name: news_mapping_concept_stock_map; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.news_mapping_concept_stock_map (
    concept text NOT NULL,
    code text NOT NULL,
    name text,
    relation_type text NOT NULL,
    confidence double precision,
    evidence text,
    source text,
    updated_at text
);


--
-- Name: news_mapping_mapping_feedback; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.news_mapping_mapping_feedback (
    feedback_id bigint NOT NULL,
    news_id text,
    code text,
    old_status text,
    new_status text,
    comment text,
    created_at text
);


--
-- Name: news_mapping_mapping_feedback_feedback_id_seq; Type: SEQUENCE; Schema: stock_app; Owner: -
--

ALTER TABLE stock_app.news_mapping_mapping_feedback ALTER COLUMN feedback_id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME stock_app.news_mapping_mapping_feedback_feedback_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: news_mapping_news_items; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.news_mapping_news_items (
    news_id text NOT NULL,
    date text,
    publish_time text,
    title text,
    content text,
    source text,
    url text,
    raw_text_hash text,
    created_at text
);


--
-- Name: news_mapping_news_stock_links; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.news_mapping_news_stock_links (
    news_id text NOT NULL,
    code text NOT NULL,
    name text,
    link_type text,
    confidence double precision,
    reason text,
    evidence text,
    mapper text NOT NULL,
    status text,
    created_at text
);


--
-- Name: news_mapping_stock_alias; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.news_mapping_stock_alias (
    alias text NOT NULL,
    code text NOT NULL,
    name text,
    source text,
    confidence double precision,
    updated_at text
);


--
-- Name: news_mapping_stock_master; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.news_mapping_stock_master (
    code text NOT NULL,
    ts_code text,
    name text,
    fullname text,
    industry text,
    area text,
    list_date text,
    aliases text,
    updated_at text
);


--
-- Name: news_stock_mapping; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.news_stock_mapping (
    mapping_id text NOT NULL,
    news_id text NOT NULL,
    stock_code text,
    stock_name text,
    industry text,
    concept text,
    relevance_score double precision,
    impact_direction text,
    impact_strength double precision,
    impact_confidence double precision,
    mapping_confidence double precision,
    mapping_method text,
    evidence_text text,
    created_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: paper_account; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_account (
    account_id text NOT NULL,
    user_id text NOT NULL,
    initial_cash double precision,
    cash double precision,
    total_assets double precision,
    daily_return double precision,
    cumulative_return double precision,
    max_drawdown double precision,
    is_paper_trading bigint DEFAULT 1,
    updated_at text DEFAULT CURRENT_TIMESTAMP,
    cumulative_deposit double precision DEFAULT 0,
    cumulative_withdrawal double precision DEFAULT 0,
    net_contribution double precision DEFAULT 0,
    absolute_profit double precision DEFAULT 0,
    time_weighted_return double precision DEFAULT 0,
    daily_fee double precision DEFAULT 0 NOT NULL,
    cumulative_fee double precision DEFAULT 0 NOT NULL,
    position_market_value double precision DEFAULT 0 NOT NULL,
    nav double precision DEFAULT 1 NOT NULL,
    drawdown double precision DEFAULT 0 NOT NULL,
    composite_nav double precision DEFAULT 1 NOT NULL
);


--
-- Name: paper_account_snapshot; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_account_snapshot (
    snapshot_id text NOT NULL,
    user_id text NOT NULL,
    account_id text NOT NULL,
    trade_date text NOT NULL,
    cash double precision DEFAULT 0 NOT NULL,
    position_market_value double precision DEFAULT 0 NOT NULL,
    total_assets double precision DEFAULT 0 NOT NULL,
    net_contribution double precision DEFAULT 0 NOT NULL,
    daily_return double precision DEFAULT 0 NOT NULL,
    cumulative_return double precision DEFAULT 0 NOT NULL,
    time_weighted_return double precision DEFAULT 0 NOT NULL,
    nav double precision DEFAULT 1 NOT NULL,
    drawdown double precision DEFAULT 0 NOT NULL,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    composite_nav double precision DEFAULT 1 NOT NULL,
    strategy_id text DEFAULT ''::text NOT NULL,
    strategy_version text DEFAULT ''::text NOT NULL,
    binding_id text DEFAULT ''::text NOT NULL,
    config_hash text DEFAULT ''::text NOT NULL,
    resolved_config_json text DEFAULT '{}'::text NOT NULL
);


--
-- Name: paper_cash_flow; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_cash_flow (
    cash_flow_id text NOT NULL,
    user_id text NOT NULL,
    effective_date text NOT NULL,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    applied_at text,
    flow_type text NOT NULL,
    amount double precision NOT NULL,
    reason text,
    status text DEFAULT 'pending'::text NOT NULL,
    source text DEFAULT 'app'::text NOT NULL,
    run_id text,
    idempotency_key text
);


--
-- Name: paper_daily_replay_audit; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_daily_replay_audit (
    daily_audit_id text NOT NULL,
    run_id text,
    user_id text,
    trade_date text,
    status text,
    original_ranking_count bigint DEFAULT 0,
    ai_adjustment_count bigint DEFAULT 0,
    candidate_count bigint DEFAULT 0,
    target_position_count bigint DEFAULT 0,
    buy_count bigint DEFAULT 0,
    sell_count bigint DEFAULT 0,
    opening_position_count bigint DEFAULT 0,
    closing_position_count bigint DEFAULT 0,
    cash double precision DEFAULT 0,
    position_market_value double precision DEFAULT 0,
    total_asset double precision DEFAULT 0,
    audit_json_path text,
    audit_md_path text,
    created_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: paper_decision_log; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_decision_log (
    decision_id text NOT NULL,
    user_id text NOT NULL,
    trade_date text,
    decision_time text,
    stock_code text,
    stock_name text,
    paper_action text,
    target_weight double precision,
    current_weight double precision,
    order_amount double precision,
    order_quantity double precision,
    executed_price double precision,
    total_fee double precision DEFAULT 0 NOT NULL,
    net_cash_change double precision DEFAULT 0 NOT NULL,
    original_rank bigint,
    original_score double precision,
    news_adjustment double precision,
    user_adjustment double precision,
    effective_news_adjustment double precision,
    combined_adjustment double precision,
    position_adjustment_ratio double precision,
    reason text,
    risk_warning text,
    triggered_rules text,
    source_decision_id text,
    job_id text,
    run_id text,
    execution_source text,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    strategy_id text DEFAULT ''::text NOT NULL,
    strategy_version text DEFAULT ''::text NOT NULL,
    binding_id text DEFAULT ''::text NOT NULL,
    config_hash text DEFAULT ''::text NOT NULL,
    resolved_config_json text DEFAULT '{}'::text NOT NULL
);


--
-- Name: paper_nav_history; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_nav_history (
    nav_id text NOT NULL,
    user_id text NOT NULL,
    account_id text NOT NULL,
    trade_date text NOT NULL,
    cash double precision DEFAULT 0 NOT NULL,
    position_market_value double precision DEFAULT 0 NOT NULL,
    total_assets double precision DEFAULT 0 NOT NULL,
    net_contribution double precision DEFAULT 0 NOT NULL,
    daily_deposit double precision DEFAULT 0 NOT NULL,
    daily_withdrawal double precision DEFAULT 0 NOT NULL,
    daily_fee double precision DEFAULT 0 NOT NULL,
    cumulative_fee double precision DEFAULT 0 NOT NULL,
    daily_profit double precision DEFAULT 0 NOT NULL,
    daily_return double precision DEFAULT 0 NOT NULL,
    cumulative_return double precision DEFAULT 0 NOT NULL,
    time_weighted_return double precision DEFAULT 0 NOT NULL,
    nav double precision DEFAULT 1 NOT NULL,
    nav_peak double precision DEFAULT 1 NOT NULL,
    drawdown double precision DEFAULT 0 NOT NULL,
    position_count bigint DEFAULT 0 NOT NULL,
    updated_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    composite_nav double precision DEFAULT 1 NOT NULL
);


--
-- Name: paper_order; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_order (
    order_id text NOT NULL,
    user_id text NOT NULL,
    account_id text,
    trade_date text,
    stock_code text,
    stock_name text,
    action text,
    target_weight double precision,
    executed_price double precision,
    quantity double precision,
    reason text,
    is_paper_trading bigint DEFAULT 1,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    decision_id text,
    decision_time text,
    paper_action text,
    current_weight double precision,
    order_amount double precision,
    risk_warning text,
    triggered_rules text,
    job_id text,
    run_id text,
    execution_source text,
    gross_amount double precision DEFAULT 0 NOT NULL,
    commission_fee double precision DEFAULT 0 NOT NULL,
    other_fee double precision DEFAULT 0 NOT NULL,
    slippage_cost double precision DEFAULT 0 NOT NULL,
    total_fee double precision DEFAULT 0 NOT NULL,
    net_cash_change double precision DEFAULT 0 NOT NULL,
    applied_buy_cost_rate double precision DEFAULT 0 NOT NULL,
    applied_sell_cost_rate double precision DEFAULT 0 NOT NULL,
    strategy_id text DEFAULT ''::text NOT NULL,
    strategy_version text DEFAULT ''::text NOT NULL,
    binding_id text DEFAULT ''::text NOT NULL,
    config_hash text DEFAULT ''::text NOT NULL,
    resolved_config_json text DEFAULT '{}'::text NOT NULL
);


--
-- Name: paper_order_reason_audit; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_order_reason_audit (
    order_reason_audit_id text NOT NULL,
    run_id text,
    user_id text,
    trade_date text,
    order_id text,
    stock_code text,
    paper_action text,
    quantity double precision DEFAULT 0,
    reason_code text,
    reason_detail text,
    audit_json_path text,
    created_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: paper_replay_run; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_replay_run (
    run_id text NOT NULL,
    user_id text,
    start_date text,
    end_date text,
    status text,
    strategy_version text,
    created_at text,
    completed_at text,
    failed_trade_dates text,
    failure_reasons text,
    continued_after_failure_count bigint DEFAULT 0,
    manifest_path text
);


--
-- Name: paper_stock_decision_audit; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_stock_decision_audit (
    stock_decision_audit_id text NOT NULL,
    run_id text,
    user_id text,
    trade_date text,
    stock_code text,
    original_rank bigint,
    final_rank bigint,
    base_weight double precision,
    stored_ai_adjustment text,
    target_weight double precision,
    target_quantity double precision,
    executed_quantity double precision,
    decision text,
    reason_code text,
    reason_detail text,
    created_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: paper_strategy_execution_history; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_strategy_execution_history (
    execution_history_id text NOT NULL,
    user_id text NOT NULL,
    account_id text NOT NULL,
    trade_date text NOT NULL,
    run_id text NOT NULL,
    strategy_id text NOT NULL,
    strategy_version text NOT NULL,
    binding_id text DEFAULT ''::text NOT NULL,
    config_hash text NOT NULL,
    resolved_config_json text DEFAULT '{}'::text NOT NULL,
    positions_before_json text DEFAULT '[]'::text NOT NULL,
    target_portfolio_json text DEFAULT '[]'::text NOT NULL,
    orders_json text DEFAULT '[]'::text NOT NULL,
    positions_after_json text DEFAULT '[]'::text NOT NULL,
    cash_before double precision DEFAULT 0 NOT NULL,
    cash_after double precision DEFAULT 0 NOT NULL,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: paper_trading_settings; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.paper_trading_settings (
    settings_id text NOT NULL,
    user_id text NOT NULL,
    entry_top_k bigint DEFAULT 10 NOT NULL,
    hold_buffer_rank bigint DEFAULT 15 NOT NULL,
    max_positions bigint DEFAULT 10 NOT NULL,
    minimum_cash_ratio double precision DEFAULT 0.05 NOT NULL,
    min_rebalance_weight_delta double precision DEFAULT 0.01 NOT NULL,
    strategy_mode text DEFAULT 'top10_score_weighted'::text NOT NULL,
    buy_cost_rate double precision DEFAULT 0.0003 NOT NULL,
    sell_cost_rate double precision DEFAULT 0.0008 NOT NULL,
    minimum_fee double precision DEFAULT 0 NOT NULL,
    slippage_rate double precision DEFAULT 0 NOT NULL,
    execution_price_type text DEFAULT 'close'::text NOT NULL,
    effective_date text DEFAULT ''::text NOT NULL,
    updated_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    target_cash_ratio double precision DEFAULT 0.05 NOT NULL,
    maximum_cash_ratio double precision DEFAULT 0.30 NOT NULL,
    target_invested_weight double precision DEFAULT 0.80 NOT NULL
);


--
-- Name: portfolio_position; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.portfolio_position (
    position_id text NOT NULL,
    user_id text NOT NULL,
    asset_code text NOT NULL,
    asset_name text,
    asset_type text,
    quantity double precision,
    cost_price double precision,
    current_price double precision,
    market_value double precision,
    profit_loss double precision,
    position_ratio double precision,
    industry text,
    updated_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: portfolio_recommendation_result; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.portfolio_recommendation_result (
    recommendation_id text NOT NULL,
    user_id text NOT NULL,
    trade_date text NOT NULL,
    stock_code text NOT NULL,
    stock_name text DEFAULT ''::text NOT NULL,
    model_name text DEFAULT ''::text NOT NULL,
    original_rank bigint,
    combined_adjustment double precision,
    target_weight double precision,
    payload_json text DEFAULT '{}'::text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL
);


--
-- Name: portfolio_risk_snapshot; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.portfolio_risk_snapshot (
    risk_snapshot_id text NOT NULL,
    user_id text NOT NULL,
    account_id text DEFAULT ''::text NOT NULL,
    as_of_date text NOT NULL,
    report_json text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL
);


--
-- Name: proposal_action_requests; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.proposal_action_requests (
    action_request_id text NOT NULL,
    proposal_id text NOT NULL,
    user_id text NOT NULL,
    session_id text NOT NULL,
    action_type text NOT NULL,
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    status text NOT NULL,
    result_json text DEFAULT '{}'::text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    completed_at text DEFAULT ''::text NOT NULL
);


--
-- Name: proposal_versions; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.proposal_versions (
    proposal_id text NOT NULL,
    version bigint NOT NULL,
    payload_hash text NOT NULL,
    payload_json text NOT NULL,
    created_at text NOT NULL,
    created_by text NOT NULL,
    revision_reason text DEFAULT ''::text NOT NULL,
    base_version bigint DEFAULT 0 NOT NULL,
    metadata_json text DEFAULT '{}'::text NOT NULL
);


--
-- Name: proposals; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.proposals (
    proposal_id text NOT NULL,
    proposal_type text NOT NULL,
    user_id text NOT NULL,
    session_id text NOT NULL,
    source_run_id text NOT NULL,
    source_request_id text NOT NULL,
    current_version bigint NOT NULL,
    status text NOT NULL,
    current_payload_hash text NOT NULL,
    approval_binding_json text DEFAULT '{}'::text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    expires_at text DEFAULT ''::text NOT NULL,
    metadata_json text DEFAULT '{}'::text NOT NULL
);


--
-- Name: rag_retrieval_log; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.rag_retrieval_log (
    retrieval_id text NOT NULL,
    query text,
    query_type text,
    user_id text,
    stock_code text,
    trade_date text,
    decision_time text,
    filters text,
    bm25_results text,
    dense_results text,
    rerank_results text,
    selected_chunk_ids text,
    returned_chunk_ids text,
    used_chunk_ids text,
    bm25_top_k bigint,
    dense_top_k bigint,
    rerank_top_k bigint,
    created_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: risk_assessment; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.risk_assessment (
    assessment_id text NOT NULL,
    user_id text NOT NULL,
    risk_score double precision,
    risk_level text,
    max_drawdown_tolerance double precision,
    single_loss_tolerance double precision,
    volatility_tolerance text,
    investment_horizon text,
    questionnaire_version text,
    assessment_time text,
    is_valid bigint DEFAULT 1,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    updated_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: runtime_data_import_audit; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.runtime_data_import_audit (
    import_id text NOT NULL,
    source_kind text NOT NULL,
    source_path text NOT NULL,
    source_sha256 text NOT NULL,
    source_row_count bigint DEFAULT 0 NOT NULL,
    imported_row_count bigint DEFAULT 0 NOT NULL,
    validation_status text NOT NULL,
    details_json text DEFAULT '{}'::text NOT NULL,
    imported_at text NOT NULL
);


--
-- Name: runtime_state_snapshot; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.runtime_state_snapshot (
    state_id text NOT NULL,
    state_kind text NOT NULL,
    user_id text DEFAULT ''::text NOT NULL,
    scope_id text DEFAULT ''::text NOT NULL,
    as_of_date text DEFAULT ''::text NOT NULL,
    payload_json text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL
);


--
-- Name: schema_migrations; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.schema_migrations (
    version text NOT NULL,
    applied_at text DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: stock_alias; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.stock_alias (
    alias_id text NOT NULL,
    stock_code text NOT NULL,
    alias_name text NOT NULL,
    alias_type text,
    confidence_base double precision,
    updated_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: stock_basic; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.stock_basic (
    stock_code text NOT NULL,
    stock_name text,
    full_name text,
    exchange text,
    list_date text,
    industry text,
    concepts text,
    main_business text,
    is_st bigint DEFAULT 0,
    market_cap double precision,
    updated_at text DEFAULT CURRENT_TIMESTAMP
);


--
-- Name: strategy_bindings; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.strategy_bindings (
    binding_id text NOT NULL,
    user_id text NOT NULL,
    account_id text NOT NULL,
    strategy_id text NOT NULL,
    strategy_version text NOT NULL,
    config_hash text NOT NULL,
    effective_from text NOT NULL,
    status text NOT NULL,
    previous_binding_id text,
    source_plan_id text NOT NULL,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    activated_at text,
    disabled_at text
);


--
-- Name: strategy_implementations; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.strategy_implementations (
    implementation_id text NOT NULL,
    proposal_id text NOT NULL,
    proposal_version bigint NOT NULL,
    user_id text NOT NULL,
    account_id text NOT NULL,
    conversation_id text NOT NULL,
    implementation_type text NOT NULL,
    artifact_root text NOT NULL,
    implementation_hash text NOT NULL,
    artifact_manifest_hash text NOT NULL,
    status text NOT NULL,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at text DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: strategy_proposal_versions; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.strategy_proposal_versions (
    proposal_id text NOT NULL,
    version bigint NOT NULL,
    base_strategy_id text NOT NULL,
    base_strategy_version text NOT NULL,
    proposal_json text NOT NULL,
    user_feedback text,
    change_summary text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    source_run_id text
);


--
-- Name: strategy_proposals; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.strategy_proposals (
    proposal_id text NOT NULL,
    user_id text NOT NULL,
    account_id text NOT NULL,
    conversation_id text NOT NULL,
    original_request text NOT NULL,
    current_version bigint DEFAULT 1 NOT NULL,
    status text DEFAULT 'draft'::text NOT NULL,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at text DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: strategy_registry; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.strategy_registry (
    strategy_id text NOT NULL,
    version text NOT NULL,
    strategy_name text NOT NULL,
    source_type text NOT NULL,
    module_path text NOT NULL,
    class_name text NOT NULL,
    config_schema_json text,
    status text DEFAULT 'draft'::text NOT NULL,
    created_by text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    code_hash text,
    validation_status text,
    backtest_status text,
    enabled_for_paper_trading bigint DEFAULT 0 NOT NULL,
    enabled_at text,
    disabled_at text,
    archived_at text,
    previous_strategy_id text,
    previous_version text,
    metadata_json text
);


--
-- Name: system_monitor_alerts; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.system_monitor_alerts (
    alert_id text NOT NULL,
    snapshot_id text NOT NULL,
    trade_date text NOT NULL,
    user_id text DEFAULT 'default'::text NOT NULL,
    layer text NOT NULL,
    metric_name text NOT NULL,
    severity text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    metric_value double precision,
    threshold_value double precision,
    message text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at text DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: system_monitor_snapshots; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.system_monitor_snapshots (
    snapshot_id text NOT NULL,
    trade_date text NOT NULL,
    user_id text DEFAULT 'default'::text NOT NULL,
    data_version text,
    model_version text,
    rag_index_version text,
    run_id text,
    portfolio_snapshot_id text,
    overall_status text DEFAULT 'normal'::text NOT NULL,
    data_metrics_json text,
    model_metrics_json text,
    rag_metrics_json text,
    agent_metrics_json text,
    portfolio_metrics_json text,
    version_info_json text,
    missing_modules_json text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at text DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: trading_behavior; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.trading_behavior (
    behavior_id text NOT NULL,
    user_id text NOT NULL,
    avg_holding_days double precision,
    turnover_rate double precision,
    avg_position_size double precision,
    preferred_industries text,
    stop_loss_behavior text,
    max_historical_loss double precision,
    trading_style text,
    updated_at text DEFAULT CURRENT_TIMESTAMP,
    avoided_industries text,
    holding_period_preference text,
    allow_high_volatility bigint DEFAULT 0
);


--
-- Name: user_feedback; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.user_feedback (
    feedback_id text NOT NULL,
    user_id text NOT NULL,
    conversation_id text,
    run_id text,
    message_id text,
    feedback_type text NOT NULL,
    rating bigint,
    comment text,
    source_id text,
    tool_name text,
    created_at text DEFAULT CURRENT_TIMESTAMP NOT NULL,
    metadata_json text
);


--
-- Name: user_profile; Type: TABLE; Schema: stock_app; Owner: -
--

CREATE TABLE stock_app.user_profile (
    user_id text NOT NULL,
    age_range text,
    income_level text,
    available_capital double precision,
    investment_experience text,
    liquidity_need text,
    created_at text DEFAULT CURRENT_TIMESTAMP,
    updated_at text DEFAULT CURRENT_TIMESTAMP,
    nickname text,
    income_stability text,
    is_active bigint DEFAULT 1,
    profile_type text DEFAULT '稳健型'::text NOT NULL,
    trading_permissions_json text DEFAULT '{}'::text NOT NULL
);


--
-- Name: agent_request_checkpoints; Type: TABLE; Schema: stock_runtime; Owner: -
--

CREATE TABLE stock_runtime.agent_request_checkpoints (
    run_id text NOT NULL,
    request_id text NOT NULL,
    status text NOT NULL,
    checkpoint_json text NOT NULL,
    updated_at text NOT NULL
);


--
-- Name: agent_run_checkpoints; Type: TABLE; Schema: stock_runtime; Owner: -
--

CREATE TABLE stock_runtime.agent_run_checkpoints (
    run_id text NOT NULL,
    session_id text NOT NULL,
    user_id text NOT NULL,
    status text NOT NULL,
    checkpoint_json text NOT NULL,
    version bigint NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL
);


--
-- Name: agent_run_slots; Type: TABLE; Schema: stock_runtime; Owner: -
--

CREATE TABLE stock_runtime.agent_run_slots (
    slot_record_id text NOT NULL,
    run_id text NOT NULL,
    task_id text NOT NULL,
    contract_id text NOT NULL,
    slot_id text NOT NULL,
    schema_id text NOT NULL,
    value_ref text NOT NULL,
    safe_summary text NOT NULL,
    value_json text,
    entity_refs_json text NOT NULL,
    provenance_refs_json text NOT NULL,
    authority_level text NOT NULL,
    freshness_time text NOT NULL,
    completeness_status text NOT NULL,
    created_at text NOT NULL
);


--
-- Name: agent_session_state_access_log; Type: TABLE; Schema: stock_runtime; Owner: -
--

CREATE TABLE stock_runtime.agent_session_state_access_log (
    access_id text NOT NULL,
    session_id text NOT NULL,
    run_id text DEFAULT ''::text NOT NULL,
    task_id text DEFAULT ''::text NOT NULL,
    agent_id text DEFAULT ''::text NOT NULL,
    operation text NOT NULL,
    query_text text DEFAULT ''::text NOT NULL,
    matched_keys_json text DEFAULT '[]'::text NOT NULL,
    created_at text NOT NULL
);


--
-- Name: agent_session_state_items; Type: TABLE; Schema: stock_runtime; Owner: -
--

CREATE TABLE stock_runtime.agent_session_state_items (
    memory_id text NOT NULL,
    session_id text NOT NULL,
    memory_key text NOT NULL,
    value_json text NOT NULL,
    value_type text DEFAULT 'json'::text NOT NULL,
    summary text DEFAULT ''::text NOT NULL,
    source_type text DEFAULT ''::text NOT NULL,
    source_ref text DEFAULT ''::text NOT NULL,
    confirmed bigint DEFAULT 0 NOT NULL,
    confidence double precision DEFAULT 0.8 NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    created_at text NOT NULL,
    updated_at text NOT NULL,
    expires_at text NOT NULL
);


--
-- Name: memory_records; Type: TABLE; Schema: stock_runtime; Owner: -
--

CREATE TABLE stock_runtime.memory_records (
    memory_id text NOT NULL,
    user_id text NOT NULL,
    conversation_id text,
    run_id text,
    task_id text,
    source_type text,
    source_id text,
    memory_type text NOT NULL,
    memory_subtype text,
    scope text,
    visibility text,
    status text,
    content text,
    summary text,
    topics_json text,
    stock_codes_json text,
    importance double precision,
    confidence double precision,
    created_at text,
    updated_at text,
    valid_from text,
    valid_until text,
    supersedes_memory_id text,
    metadata_json text,
    context_refs_json text,
    message_refs_json text,
    artifact_refs_json text,
    approval_refs_json text,
    source_refs_json text
);


--
-- Name: task_events; Type: TABLE; Schema: stock_runtime; Owner: -
--

CREATE TABLE stock_runtime.task_events (
    sequence bigint NOT NULL,
    task_id text NOT NULL,
    event_type text NOT NULL,
    data_json text DEFAULT '{}'::text NOT NULL,
    created_at text NOT NULL
);


--
-- Name: task_events_sequence_seq; Type: SEQUENCE; Schema: stock_runtime; Owner: -
--

ALTER TABLE stock_runtime.task_events ALTER COLUMN sequence ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME stock_runtime.task_events_sequence_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: task_runs; Type: TABLE; Schema: stock_runtime; Owner: -
--

CREATE TABLE stock_runtime.task_runs (
    task_id text NOT NULL,
    task_type text NOT NULL,
    status text NOT NULL,
    owner_id text DEFAULT ''::text NOT NULL,
    session_id text DEFAULT ''::text NOT NULL,
    request_json text NOT NULL,
    metadata_json text DEFAULT '{}'::text NOT NULL,
    result_json text,
    error_json text,
    progress double precision DEFAULT 0 NOT NULL,
    message text DEFAULT ''::text NOT NULL,
    created_at text NOT NULL,
    started_at text,
    finished_at text,
    updated_at text NOT NULL,
    timeout_seconds bigint DEFAULT 99600 NOT NULL,
    max_retries bigint DEFAULT 0 NOT NULL,
    attempt bigint DEFAULT 0 NOT NULL,
    cancel_requested bigint DEFAULT 0 NOT NULL,
    worker_pid bigint,
    acknowledged_at text
);


--
-- Name: action_approvals action_approvals_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.action_approvals
    ADD CONSTRAINT action_approvals_pkey PRIMARY KEY (approval_id);


--
-- Name: action_commits action_commits_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.action_commits
    ADD CONSTRAINT action_commits_pkey PRIMARY KEY (commit_id);


--
-- Name: action_proposals action_proposals_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.action_proposals
    ADD CONSTRAINT action_proposals_pkey PRIMARY KEY (plan_id);


--
-- Name: agent_action_log agent_action_log_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_action_log
    ADD CONSTRAINT agent_action_log_pkey PRIMARY KEY (action_id);


--
-- Name: agent_confirmation_log agent_confirmation_log_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_confirmation_log
    ADD CONSTRAINT agent_confirmation_log_pkey PRIMARY KEY (confirmation_id);


--
-- Name: agent_decision_log agent_decision_log_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_decision_log
    ADD CONSTRAINT agent_decision_log_pkey PRIMARY KEY (decision_id);


--
-- Name: agent_rule agent_rule_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_rule
    ADD CONSTRAINT agent_rule_pkey PRIMARY KEY (rule_id);


--
-- Name: agent_runs agent_runs_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_runs
    ADD CONSTRAINT agent_runs_pkey PRIMARY KEY (run_id);


--
-- Name: agent_sandbox_runs agent_sandbox_runs_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_sandbox_runs
    ADD CONSTRAINT agent_sandbox_runs_pkey PRIMARY KEY (sandbox_run_id);


--
-- Name: agent_sources agent_sources_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_sources
    ADD CONSTRAINT agent_sources_pkey PRIMARY KEY (source_id);


--
-- Name: agent_steps agent_steps_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_steps
    ADD CONSTRAINT agent_steps_pkey PRIMARY KEY (run_id, step_id);


--
-- Name: agent_tool_call_log agent_tool_call_log_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_tool_call_log
    ADD CONSTRAINT agent_tool_call_log_pkey PRIMARY KEY (call_id);


--
-- Name: agent_tool_calls agent_tool_calls_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_tool_calls
    ADD CONSTRAINT agent_tool_calls_pkey PRIMARY KEY (tool_call_id);


--
-- Name: agent_turn_summaries agent_turn_summaries_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.agent_turn_summaries
    ADD CONSTRAINT agent_turn_summaries_pkey PRIMARY KEY (turn_id);


--
-- Name: artifacts artifacts_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.artifacts
    ADD CONSTRAINT artifacts_pkey PRIMARY KEY (artifact_id);


--
-- Name: backtest_evaluation backtest_evaluation_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.backtest_evaluation
    ADD CONSTRAINT backtest_evaluation_pkey PRIMARY KEY (eval_id);


--
-- Name: conversation_summaries conversation_summaries_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.conversation_summaries
    ADD CONSTRAINT conversation_summaries_pkey PRIMARY KEY (summary_id);


--
-- Name: conversations conversations_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.conversations
    ADD CONSTRAINT conversations_pkey PRIMARY KEY (conversation_id);


--
-- Name: industry_event_rule industry_event_rule_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.industry_event_rule
    ADD CONSTRAINT industry_event_rule_pkey PRIMARY KEY (rule_id);


--
-- Name: investment_goal investment_goal_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.investment_goal
    ADD CONSTRAINT investment_goal_pkey PRIMARY KEY (goal_id);


--
-- Name: market_data_daily market_data_daily_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.market_data_daily
    ADD CONSTRAINT market_data_daily_pkey PRIMARY KEY (trade_date, stock_code);


--
-- Name: memory_items memory_items_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.memory_items
    ADD CONSTRAINT memory_items_pkey PRIMARY KEY (memory_id);


--
-- Name: memory_links memory_links_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.memory_links
    ADD CONSTRAINT memory_links_pkey PRIMARY KEY (link_id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (message_id);


--
-- Name: model_prediction model_prediction_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.model_prediction
    ADD CONSTRAINT model_prediction_pkey PRIMARY KEY (prediction_id);


--
-- Name: news_chunk news_chunk_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.news_chunk
    ADD CONSTRAINT news_chunk_pkey PRIMARY KEY (chunk_id);


--
-- Name: news_embedding news_embedding_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.news_embedding
    ADD CONSTRAINT news_embedding_pkey PRIMARY KEY (embedding_id);


--
-- Name: news_event news_event_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.news_event
    ADD CONSTRAINT news_event_pkey PRIMARY KEY (news_id);


--
-- Name: news_mapping_concept_stock_map news_mapping_concept_stock_map_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.news_mapping_concept_stock_map
    ADD CONSTRAINT news_mapping_concept_stock_map_pkey PRIMARY KEY (concept, code, relation_type);


--
-- Name: news_mapping_mapping_feedback news_mapping_mapping_feedback_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.news_mapping_mapping_feedback
    ADD CONSTRAINT news_mapping_mapping_feedback_pkey PRIMARY KEY (feedback_id);


--
-- Name: news_mapping_news_items news_mapping_news_items_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.news_mapping_news_items
    ADD CONSTRAINT news_mapping_news_items_pkey PRIMARY KEY (news_id);


--
-- Name: news_mapping_news_stock_links news_mapping_news_stock_links_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.news_mapping_news_stock_links
    ADD CONSTRAINT news_mapping_news_stock_links_pkey PRIMARY KEY (news_id, code, mapper);


--
-- Name: news_mapping_stock_alias news_mapping_stock_alias_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.news_mapping_stock_alias
    ADD CONSTRAINT news_mapping_stock_alias_pkey PRIMARY KEY (alias, code);


--
-- Name: news_mapping_stock_master news_mapping_stock_master_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.news_mapping_stock_master
    ADD CONSTRAINT news_mapping_stock_master_pkey PRIMARY KEY (code);


--
-- Name: news_stock_mapping news_stock_mapping_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.news_stock_mapping
    ADD CONSTRAINT news_stock_mapping_pkey PRIMARY KEY (mapping_id);


--
-- Name: paper_account paper_account_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_account
    ADD CONSTRAINT paper_account_pkey PRIMARY KEY (account_id);


--
-- Name: paper_account_snapshot paper_account_snapshot_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_account_snapshot
    ADD CONSTRAINT paper_account_snapshot_pkey PRIMARY KEY (snapshot_id);


--
-- Name: paper_cash_flow paper_cash_flow_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_cash_flow
    ADD CONSTRAINT paper_cash_flow_pkey PRIMARY KEY (cash_flow_id);


--
-- Name: paper_daily_replay_audit paper_daily_replay_audit_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_daily_replay_audit
    ADD CONSTRAINT paper_daily_replay_audit_pkey PRIMARY KEY (daily_audit_id);


--
-- Name: paper_decision_log paper_decision_log_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_decision_log
    ADD CONSTRAINT paper_decision_log_pkey PRIMARY KEY (decision_id);


--
-- Name: paper_nav_history paper_nav_history_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_nav_history
    ADD CONSTRAINT paper_nav_history_pkey PRIMARY KEY (nav_id);


--
-- Name: paper_order paper_order_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_order
    ADD CONSTRAINT paper_order_pkey PRIMARY KEY (order_id);


--
-- Name: paper_order_reason_audit paper_order_reason_audit_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_order_reason_audit
    ADD CONSTRAINT paper_order_reason_audit_pkey PRIMARY KEY (order_reason_audit_id);


--
-- Name: paper_replay_run paper_replay_run_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_replay_run
    ADD CONSTRAINT paper_replay_run_pkey PRIMARY KEY (run_id);


--
-- Name: paper_stock_decision_audit paper_stock_decision_audit_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_stock_decision_audit
    ADD CONSTRAINT paper_stock_decision_audit_pkey PRIMARY KEY (stock_decision_audit_id);


--
-- Name: paper_strategy_execution_history paper_strategy_execution_history_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_strategy_execution_history
    ADD CONSTRAINT paper_strategy_execution_history_pkey PRIMARY KEY (execution_history_id);


--
-- Name: paper_trading_settings paper_trading_settings_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.paper_trading_settings
    ADD CONSTRAINT paper_trading_settings_pkey PRIMARY KEY (settings_id);


--
-- Name: portfolio_position portfolio_position_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.portfolio_position
    ADD CONSTRAINT portfolio_position_pkey PRIMARY KEY (position_id);


--
-- Name: portfolio_recommendation_result portfolio_recommendation_result_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.portfolio_recommendation_result
    ADD CONSTRAINT portfolio_recommendation_result_pkey PRIMARY KEY (recommendation_id);


--
-- Name: portfolio_risk_snapshot portfolio_risk_snapshot_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.portfolio_risk_snapshot
    ADD CONSTRAINT portfolio_risk_snapshot_pkey PRIMARY KEY (risk_snapshot_id);


--
-- Name: proposal_action_requests proposal_action_requests_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.proposal_action_requests
    ADD CONSTRAINT proposal_action_requests_pkey PRIMARY KEY (action_request_id);


--
-- Name: proposal_versions proposal_versions_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.proposal_versions
    ADD CONSTRAINT proposal_versions_pkey PRIMARY KEY (proposal_id, version);


--
-- Name: proposals proposals_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.proposals
    ADD CONSTRAINT proposals_pkey PRIMARY KEY (proposal_id);


--
-- Name: rag_retrieval_log rag_retrieval_log_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.rag_retrieval_log
    ADD CONSTRAINT rag_retrieval_log_pkey PRIMARY KEY (retrieval_id);


--
-- Name: risk_assessment risk_assessment_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.risk_assessment
    ADD CONSTRAINT risk_assessment_pkey PRIMARY KEY (assessment_id);


--
-- Name: runtime_data_import_audit runtime_data_import_audit_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.runtime_data_import_audit
    ADD CONSTRAINT runtime_data_import_audit_pkey PRIMARY KEY (import_id);


--
-- Name: runtime_state_snapshot runtime_state_snapshot_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.runtime_state_snapshot
    ADD CONSTRAINT runtime_state_snapshot_pkey PRIMARY KEY (state_id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: stock_alias stock_alias_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.stock_alias
    ADD CONSTRAINT stock_alias_pkey PRIMARY KEY (alias_id);


--
-- Name: stock_basic stock_basic_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.stock_basic
    ADD CONSTRAINT stock_basic_pkey PRIMARY KEY (stock_code);


--
-- Name: strategy_bindings strategy_bindings_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.strategy_bindings
    ADD CONSTRAINT strategy_bindings_pkey PRIMARY KEY (binding_id);


--
-- Name: strategy_implementations strategy_implementations_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.strategy_implementations
    ADD CONSTRAINT strategy_implementations_pkey PRIMARY KEY (implementation_id);


--
-- Name: strategy_proposal_versions strategy_proposal_versions_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.strategy_proposal_versions
    ADD CONSTRAINT strategy_proposal_versions_pkey PRIMARY KEY (proposal_id, version);


--
-- Name: strategy_proposals strategy_proposals_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.strategy_proposals
    ADD CONSTRAINT strategy_proposals_pkey PRIMARY KEY (proposal_id);


--
-- Name: strategy_registry strategy_registry_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.strategy_registry
    ADD CONSTRAINT strategy_registry_pkey PRIMARY KEY (strategy_id, version);


--
-- Name: system_monitor_alerts system_monitor_alerts_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.system_monitor_alerts
    ADD CONSTRAINT system_monitor_alerts_pkey PRIMARY KEY (alert_id);


--
-- Name: system_monitor_snapshots system_monitor_snapshots_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.system_monitor_snapshots
    ADD CONSTRAINT system_monitor_snapshots_pkey PRIMARY KEY (snapshot_id);


--
-- Name: trading_behavior trading_behavior_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.trading_behavior
    ADD CONSTRAINT trading_behavior_pkey PRIMARY KEY (behavior_id);


--
-- Name: user_feedback user_feedback_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.user_feedback
    ADD CONSTRAINT user_feedback_pkey PRIMARY KEY (feedback_id);


--
-- Name: user_profile user_profile_pkey; Type: CONSTRAINT; Schema: stock_app; Owner: -
--

ALTER TABLE ONLY stock_app.user_profile
    ADD CONSTRAINT user_profile_pkey PRIMARY KEY (user_id);


--
-- Name: agent_request_checkpoints agent_request_checkpoints_pkey; Type: CONSTRAINT; Schema: stock_runtime; Owner: -
--

ALTER TABLE ONLY stock_runtime.agent_request_checkpoints
    ADD CONSTRAINT agent_request_checkpoints_pkey PRIMARY KEY (run_id, request_id);


--
-- Name: agent_run_checkpoints agent_run_checkpoints_pkey; Type: CONSTRAINT; Schema: stock_runtime; Owner: -
--

ALTER TABLE ONLY stock_runtime.agent_run_checkpoints
    ADD CONSTRAINT agent_run_checkpoints_pkey PRIMARY KEY (run_id);


--
-- Name: agent_run_slots agent_run_slots_pkey; Type: CONSTRAINT; Schema: stock_runtime; Owner: -
--

ALTER TABLE ONLY stock_runtime.agent_run_slots
    ADD CONSTRAINT agent_run_slots_pkey PRIMARY KEY (slot_record_id);


--
-- Name: agent_session_state_access_log agent_session_state_access_log_pkey; Type: CONSTRAINT; Schema: stock_runtime; Owner: -
--

ALTER TABLE ONLY stock_runtime.agent_session_state_access_log
    ADD CONSTRAINT agent_session_state_access_log_pkey PRIMARY KEY (access_id);


--
-- Name: agent_session_state_items agent_session_state_items_pkey; Type: CONSTRAINT; Schema: stock_runtime; Owner: -
--

ALTER TABLE ONLY stock_runtime.agent_session_state_items
    ADD CONSTRAINT agent_session_state_items_pkey PRIMARY KEY (memory_id);


--
-- Name: memory_records memory_records_pkey; Type: CONSTRAINT; Schema: stock_runtime; Owner: -
--

ALTER TABLE ONLY stock_runtime.memory_records
    ADD CONSTRAINT memory_records_pkey PRIMARY KEY (memory_id);


--
-- Name: task_events task_events_pkey; Type: CONSTRAINT; Schema: stock_runtime; Owner: -
--

ALTER TABLE ONLY stock_runtime.task_events
    ADD CONSTRAINT task_events_pkey PRIMARY KEY (sequence);


--
-- Name: task_runs task_runs_pkey; Type: CONSTRAINT; Schema: stock_runtime; Owner: -
--

ALTER TABLE ONLY stock_runtime.task_runs
    ADD CONSTRAINT task_runs_pkey PRIMARY KEY (task_id);


--
-- Name: action_approvals__idx_action_approvals_plan_status; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX action_approvals__idx_action_approvals_plan_status ON stock_app.action_approvals USING btree (plan_id, status);


--
-- Name: action_commits__idx_action_commits_idempotency; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX action_commits__idx_action_commits_idempotency ON stock_app.action_commits USING btree (idempotency_key);


--
-- Name: action_proposals__idx_action_proposals_user_status; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX action_proposals__idx_action_proposals_user_status ON stock_app.action_proposals USING btree (user_id, status, created_at);


--
-- Name: agent_action_log__idx_agent_action_log_user_time; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_action_log__idx_agent_action_log_user_time ON stock_app.agent_action_log USING btree (user_id, created_at);


--
-- Name: agent_confirmation_log__idx_agent_confirmation_log_user_plan; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_confirmation_log__idx_agent_confirmation_log_user_plan ON stock_app.agent_confirmation_log USING btree (user_id, plan_id);


--
-- Name: agent_decision_log__idx_agent_decision_log_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_decision_log__idx_agent_decision_log_date ON stock_app.agent_decision_log USING btree (trade_date);


--
-- Name: agent_decision_log__idx_agent_decision_log_stock; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_decision_log__idx_agent_decision_log_stock ON stock_app.agent_decision_log USING btree (stock_code);


--
-- Name: agent_decision_log__idx_agent_decision_log_user; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_decision_log__idx_agent_decision_log_user ON stock_app.agent_decision_log USING btree (user_id);


--
-- Name: agent_runs__idx_agent_runs_user_status; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_runs__idx_agent_runs_user_status ON stock_app.agent_runs USING btree (user_id, status, created_at);


--
-- Name: agent_sandbox_runs__idx_agent_sandbox_runs_run_step; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_sandbox_runs__idx_agent_sandbox_runs_run_step ON stock_app.agent_sandbox_runs USING btree (run_id, step_id);


--
-- Name: agent_sandbox_runs__idx_agent_sandbox_runs_user_status; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_sandbox_runs__idx_agent_sandbox_runs_user_status ON stock_app.agent_sandbox_runs USING btree (user_id, status, started_at);


--
-- Name: agent_sources__idx_agent_sources_user_type; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_sources__idx_agent_sources_user_type ON stock_app.agent_sources USING btree (user_id, source_type, retrieved_at);


--
-- Name: agent_steps__idx_agent_steps_record_id; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX agent_steps__idx_agent_steps_record_id ON stock_app.agent_steps USING btree (step_record_id);


--
-- Name: agent_steps__idx_agent_steps_run_started; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_steps__idx_agent_steps_run_started ON stock_app.agent_steps USING btree (run_id, started_at, step_record_id);


--
-- Name: agent_steps__idx_agent_steps_run_status; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_steps__idx_agent_steps_run_status ON stock_app.agent_steps USING btree (run_id, status);


--
-- Name: agent_tool_call_log__idx_agent_tool_call_log_user_time; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_tool_call_log__idx_agent_tool_call_log_user_time ON stock_app.agent_tool_call_log USING btree (user_id, created_at);


--
-- Name: agent_tool_calls__idx_agent_tool_calls_run_step; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_tool_calls__idx_agent_tool_calls_run_step ON stock_app.agent_tool_calls USING btree (run_id, step_id);


--
-- Name: agent_tool_calls__idx_agent_tool_calls_user_tool; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_tool_calls__idx_agent_tool_calls_user_tool ON stock_app.agent_tool_calls USING btree (user_id, tool_name, started_at);


--
-- Name: agent_turn_summaries__idx_turn_summaries_user_created; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX agent_turn_summaries__idx_turn_summaries_user_created ON stock_app.agent_turn_summaries USING btree (user_id, created_at);


--
-- Name: artifacts__idx_artifacts_user_type; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX artifacts__idx_artifacts_user_type ON stock_app.artifacts USING btree (user_id, artifact_type, created_at);


--
-- Name: conversation_summaries__idx_conversation_summaries_user_stat; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX conversation_summaries__idx_conversation_summaries_user_stat ON stock_app.conversation_summaries USING btree (user_id, status, updated_at);


--
-- Name: conversations__idx_conversations_user_time; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX conversations__idx_conversations_user_time ON stock_app.conversations USING btree (user_id, updated_at);


--
-- Name: idx_news_mapping_concept_map_concept; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX idx_news_mapping_concept_map_concept ON stock_app.news_mapping_concept_stock_map USING btree (concept);


--
-- Name: idx_news_mapping_items_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX idx_news_mapping_items_date ON stock_app.news_mapping_news_items USING btree (date);


--
-- Name: idx_news_mapping_items_hash; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX idx_news_mapping_items_hash ON stock_app.news_mapping_news_items USING btree (raw_text_hash);


--
-- Name: idx_news_mapping_links_code; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX idx_news_mapping_links_code ON stock_app.news_mapping_news_stock_links USING btree (code);


--
-- Name: idx_news_mapping_links_news; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX idx_news_mapping_links_news ON stock_app.news_mapping_news_stock_links USING btree (news_id);


--
-- Name: idx_news_mapping_links_status; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX idx_news_mapping_links_status ON stock_app.news_mapping_news_stock_links USING btree (status);


--
-- Name: idx_news_mapping_stock_alias_alias; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX idx_news_mapping_stock_alias_alias ON stock_app.news_mapping_stock_alias USING btree (alias);


--
-- Name: idx_news_mapping_stock_alias_code; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX idx_news_mapping_stock_alias_code ON stock_app.news_mapping_stock_alias USING btree (code);


--
-- Name: investment_goal__idx_investment_goal_user; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX investment_goal__idx_investment_goal_user ON stock_app.investment_goal USING btree (user_id);


--
-- Name: memory_items__idx_memory_items_user_type_status; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX memory_items__idx_memory_items_user_type_status ON stock_app.memory_items USING btree (user_id, memory_type, status, updated_at);


--
-- Name: memory_links__idx_memory_links_memory; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX memory_links__idx_memory_links_memory ON stock_app.memory_links USING btree (memory_id, linked_type);


--
-- Name: messages__idx_messages_conversation_time; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX messages__idx_messages_conversation_time ON stock_app.messages USING btree (conversation_id, created_at);


--
-- Name: model_prediction__idx_model_prediction_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX model_prediction__idx_model_prediction_date ON stock_app.model_prediction USING btree (trade_date);


--
-- Name: model_prediction__idx_model_prediction_latest; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX model_prediction__idx_model_prediction_latest ON stock_app.model_prediction USING btree (source_kind, trade_date, model_name, pred_rank);


--
-- Name: model_prediction__idx_model_prediction_stock; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX model_prediction__idx_model_prediction_stock ON stock_app.model_prediction USING btree (stock_code);


--
-- Name: news_chunk__idx_news_chunk_event_type; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_chunk__idx_news_chunk_event_type ON stock_app.news_chunk USING btree (event_type);


--
-- Name: news_chunk__idx_news_chunk_industry; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_chunk__idx_news_chunk_industry ON stock_app.news_chunk USING btree (industry);


--
-- Name: news_chunk__idx_news_chunk_news; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_chunk__idx_news_chunk_news ON stock_app.news_chunk USING btree (news_id);


--
-- Name: news_chunk__idx_news_chunk_publish_time; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_chunk__idx_news_chunk_publish_time ON stock_app.news_chunk USING btree (publish_time);


--
-- Name: news_chunk__idx_news_chunk_stock; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_chunk__idx_news_chunk_stock ON stock_app.news_chunk USING btree (stock_code);


--
-- Name: news_chunk__idx_news_chunk_trade_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_chunk__idx_news_chunk_trade_date ON stock_app.news_chunk USING btree (trade_date);


--
-- Name: news_event__idx_news_event_publish_time; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_event__idx_news_event_publish_time ON stock_app.news_event USING btree (publish_time);


--
-- Name: news_event__idx_news_event_trade_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_event__idx_news_event_trade_date ON stock_app.news_event USING btree (trade_date);


--
-- Name: news_mapping_concept_stock_map__idx_concept_stock_map_concep; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_mapping_concept_stock_map__idx_concept_stock_map_concep ON stock_app.news_mapping_concept_stock_map USING btree (concept);


--
-- Name: news_mapping_news_items__idx_news_items_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_mapping_news_items__idx_news_items_date ON stock_app.news_mapping_news_items USING btree (date);


--
-- Name: news_mapping_news_items__idx_news_items_hash; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX news_mapping_news_items__idx_news_items_hash ON stock_app.news_mapping_news_items USING btree (raw_text_hash);


--
-- Name: news_mapping_news_stock_links__idx_news_stock_links_code; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_mapping_news_stock_links__idx_news_stock_links_code ON stock_app.news_mapping_news_stock_links USING btree (code);


--
-- Name: news_mapping_news_stock_links__idx_news_stock_links_news; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_mapping_news_stock_links__idx_news_stock_links_news ON stock_app.news_mapping_news_stock_links USING btree (news_id);


--
-- Name: news_mapping_news_stock_links__idx_news_stock_links_status; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_mapping_news_stock_links__idx_news_stock_links_status ON stock_app.news_mapping_news_stock_links USING btree (status);


--
-- Name: news_mapping_stock_alias__idx_stock_alias_alias; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_mapping_stock_alias__idx_stock_alias_alias ON stock_app.news_mapping_stock_alias USING btree (alias);


--
-- Name: news_mapping_stock_alias__idx_stock_alias_code; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_mapping_stock_alias__idx_stock_alias_code ON stock_app.news_mapping_stock_alias USING btree (code);


--
-- Name: news_stock_mapping__idx_news_stock_mapping_news; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_stock_mapping__idx_news_stock_mapping_news ON stock_app.news_stock_mapping USING btree (news_id);


--
-- Name: news_stock_mapping__idx_news_stock_mapping_stock; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX news_stock_mapping__idx_news_stock_mapping_stock ON stock_app.news_stock_mapping USING btree (stock_code);


--
-- Name: paper_account__idx_paper_account_user; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_account__idx_paper_account_user ON stock_app.paper_account USING btree (user_id);


--
-- Name: paper_account_snapshot__idx_paper_account_snapshot_user_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_account_snapshot__idx_paper_account_snapshot_user_date ON stock_app.paper_account_snapshot USING btree (user_id, trade_date);


--
-- Name: paper_cash_flow__idx_paper_cash_flow_idempotency; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX paper_cash_flow__idx_paper_cash_flow_idempotency ON stock_app.paper_cash_flow USING btree (user_id, idempotency_key);


--
-- Name: paper_cash_flow__idx_paper_cash_flow_user_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_cash_flow__idx_paper_cash_flow_user_date ON stock_app.paper_cash_flow USING btree (user_id, effective_date);


--
-- Name: paper_daily_replay_audit__idx_paper_daily_replay_audit_run; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_daily_replay_audit__idx_paper_daily_replay_audit_run ON stock_app.paper_daily_replay_audit USING btree (run_id);


--
-- Name: paper_daily_replay_audit__idx_paper_daily_replay_audit_user_; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_daily_replay_audit__idx_paper_daily_replay_audit_user_ ON stock_app.paper_daily_replay_audit USING btree (user_id, trade_date);


--
-- Name: paper_decision_log__idx_paper_decision_log_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_decision_log__idx_paper_decision_log_date ON stock_app.paper_decision_log USING btree (trade_date);


--
-- Name: paper_decision_log__idx_paper_decision_log_user; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_decision_log__idx_paper_decision_log_user ON stock_app.paper_decision_log USING btree (user_id);


--
-- Name: paper_nav_history__idx_paper_nav_history_user_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_nav_history__idx_paper_nav_history_user_date ON stock_app.paper_nav_history USING btree (user_id, trade_date);


--
-- Name: paper_order__idx_paper_order_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_order__idx_paper_order_date ON stock_app.paper_order USING btree (trade_date);


--
-- Name: paper_order__idx_paper_order_stock; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_order__idx_paper_order_stock ON stock_app.paper_order USING btree (stock_code);


--
-- Name: paper_order__idx_paper_order_user; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_order__idx_paper_order_user ON stock_app.paper_order USING btree (user_id);


--
-- Name: paper_order_reason_audit__idx_paper_order_reason_audit_order; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_order_reason_audit__idx_paper_order_reason_audit_order ON stock_app.paper_order_reason_audit USING btree (order_id);


--
-- Name: paper_order_reason_audit__idx_paper_order_reason_audit_run; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_order_reason_audit__idx_paper_order_reason_audit_run ON stock_app.paper_order_reason_audit USING btree (run_id);


--
-- Name: paper_stock_decision_audit__idx_paper_stock_decision_audit_r; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_stock_decision_audit__idx_paper_stock_decision_audit_r ON stock_app.paper_stock_decision_audit USING btree (run_id);


--
-- Name: paper_stock_decision_audit__idx_paper_stock_decision_audit_s; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_stock_decision_audit__idx_paper_stock_decision_audit_s ON stock_app.paper_stock_decision_audit USING btree (stock_code);


--
-- Name: paper_strategy_execution_history__idx_strategy_execution_his; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_strategy_execution_history__idx_strategy_execution_his ON stock_app.paper_strategy_execution_history USING btree (binding_id, trade_date);


--
-- Name: paper_trading_settings__idx_paper_trading_settings_user; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX paper_trading_settings__idx_paper_trading_settings_user ON stock_app.paper_trading_settings USING btree (user_id, effective_date);


--
-- Name: portfolio_position__idx_portfolio_position_asset; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX portfolio_position__idx_portfolio_position_asset ON stock_app.portfolio_position USING btree (asset_code);


--
-- Name: portfolio_position__idx_portfolio_position_user; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX portfolio_position__idx_portfolio_position_user ON stock_app.portfolio_position USING btree (user_id);


--
-- Name: portfolio_recommendation_result__idx_portfolio_recommendatio; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX portfolio_recommendation_result__idx_portfolio_recommendatio ON stock_app.portfolio_recommendation_result USING btree (user_id, trade_date, original_rank);


--
-- Name: portfolio_recommendation_result__uq_portfolio_recommendation; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX portfolio_recommendation_result__uq_portfolio_recommendation ON stock_app.portfolio_recommendation_result USING btree (user_id, trade_date, stock_code, model_name);


--
-- Name: portfolio_risk_snapshot__idx_portfolio_risk_snapshot_latest; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX portfolio_risk_snapshot__idx_portfolio_risk_snapshot_latest ON stock_app.portfolio_risk_snapshot USING btree (user_id, as_of_date);


--
-- Name: portfolio_risk_snapshot__uq_portfolio_risk_snapshot_user_id_; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX portfolio_risk_snapshot__uq_portfolio_risk_snapshot_user_id_ ON stock_app.portfolio_risk_snapshot USING btree (user_id, as_of_date);


--
-- Name: proposal_action_requests__idx_proposal_action_proposal; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX proposal_action_requests__idx_proposal_action_proposal ON stock_app.proposal_action_requests USING btree (proposal_id, created_at);


--
-- Name: proposal_action_requests__uq_proposal_action_requests_user_i; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX proposal_action_requests__uq_proposal_action_requests_user_i ON stock_app.proposal_action_requests USING btree (user_id, idempotency_key);


--
-- Name: proposals__idx_proposals_owner_status; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX proposals__idx_proposals_owner_status ON stock_app.proposals USING btree (user_id, session_id, status, updated_at);


--
-- Name: proposals__idx_proposals_source_identity; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX proposals__idx_proposals_source_identity ON stock_app.proposals USING btree (user_id, source_run_id, source_request_id);


--
-- Name: risk_assessment__idx_risk_assessment_user; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX risk_assessment__idx_risk_assessment_user ON stock_app.risk_assessment USING btree (user_id);


--
-- Name: runtime_data_import_audit__uq_runtime_data_import_audit_sour; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX runtime_data_import_audit__uq_runtime_data_import_audit_sour ON stock_app.runtime_data_import_audit USING btree (source_kind, source_sha256);


--
-- Name: runtime_state_snapshot__idx_runtime_state_lookup; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX runtime_state_snapshot__idx_runtime_state_lookup ON stock_app.runtime_state_snapshot USING btree (state_kind, user_id, scope_id, as_of_date);


--
-- Name: runtime_state_snapshot__uq_runtime_state_snapshot_state_kind; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX runtime_state_snapshot__uq_runtime_state_snapshot_state_kind ON stock_app.runtime_state_snapshot USING btree (state_kind, user_id, scope_id);


--
-- Name: stock_alias__idx_stock_alias_code; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX stock_alias__idx_stock_alias_code ON stock_app.stock_alias USING btree (stock_code);


--
-- Name: stock_alias__idx_stock_alias_name; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX stock_alias__idx_stock_alias_name ON stock_app.stock_alias USING btree (alias_name);


--
-- Name: strategy_bindings__idx_strategy_bindings_scope_history; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX strategy_bindings__idx_strategy_bindings_scope_history ON stock_app.strategy_bindings USING btree (user_id, account_id, effective_from, created_at);


--
-- Name: strategy_bindings__uq_strategy_bindings_active_account; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX strategy_bindings__uq_strategy_bindings_active_account ON stock_app.strategy_bindings USING btree (user_id, account_id);


--
-- Name: strategy_implementations__idx_strategy_implementations_scope; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX strategy_implementations__idx_strategy_implementations_scope ON stock_app.strategy_implementations USING btree (user_id, account_id, conversation_id, status);


--
-- Name: strategy_implementations__uq_strategy_implementations_propos; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE UNIQUE INDEX strategy_implementations__uq_strategy_implementations_propos ON stock_app.strategy_implementations USING btree (proposal_id, proposal_version);


--
-- Name: strategy_proposal_versions__idx_strategy_proposal_versions_c; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX strategy_proposal_versions__idx_strategy_proposal_versions_c ON stock_app.strategy_proposal_versions USING btree (proposal_id, created_at);


--
-- Name: strategy_proposals__idx_strategy_proposals_scope; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX strategy_proposals__idx_strategy_proposals_scope ON stock_app.strategy_proposals USING btree (user_id, account_id, conversation_id, status, updated_at);


--
-- Name: strategy_registry__idx_strategy_registry_status; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX strategy_registry__idx_strategy_registry_status ON stock_app.strategy_registry USING btree (status, enabled_for_paper_trading, created_at);


--
-- Name: system_monitor_alerts__idx_system_monitor_alerts_snapshot; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX system_monitor_alerts__idx_system_monitor_alerts_snapshot ON stock_app.system_monitor_alerts USING btree (snapshot_id, severity);


--
-- Name: system_monitor_snapshots__idx_system_monitor_snapshots_date; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX system_monitor_snapshots__idx_system_monitor_snapshots_date ON stock_app.system_monitor_snapshots USING btree (trade_date, user_id);


--
-- Name: user_feedback__idx_user_feedback_user_type; Type: INDEX; Schema: stock_app; Owner: -
--

CREATE INDEX user_feedback__idx_user_feedback_user_type ON stock_app.user_feedback USING btree (user_id, feedback_type, created_at);


--
-- Name: agent_run_checkpoints__idx_agent_run_checkpoints_session_sta; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX agent_run_checkpoints__idx_agent_run_checkpoints_session_sta ON stock_runtime.agent_run_checkpoints USING btree (session_id, status);


--
-- Name: agent_run_slots__idx_agent_run_slots_run_slot; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX agent_run_slots__idx_agent_run_slots_run_slot ON stock_runtime.agent_run_slots USING btree (run_id, slot_id);


--
-- Name: agent_session_state_access_log__idx_agent_session_state_acce; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX agent_session_state_access_log__idx_agent_session_state_acce ON stock_runtime.agent_session_state_access_log USING btree (session_id, run_id, task_id, created_at);


--
-- Name: agent_session_state_items__idx_agent_session_state_expiry; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX agent_session_state_items__idx_agent_session_state_expiry ON stock_runtime.agent_session_state_items USING btree (expires_at, status);


--
-- Name: agent_session_state_items__idx_agent_session_state_lookup; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX agent_session_state_items__idx_agent_session_state_lookup ON stock_runtime.agent_session_state_items USING btree (session_id, memory_key, status, version);


--
-- Name: agent_session_state_items__uq_agent_session_state_items_sess; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE UNIQUE INDEX agent_session_state_items__uq_agent_session_state_items_sess ON stock_runtime.agent_session_state_items USING btree (session_id, memory_key, version);


--
-- Name: idx_agent_run_checkpoints_session_status; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX idx_agent_run_checkpoints_session_status ON stock_runtime.agent_run_checkpoints USING btree (session_id, status);


--
-- Name: idx_agent_session_state_access_task; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX idx_agent_session_state_access_task ON stock_runtime.agent_session_state_access_log USING btree (session_id, run_id, task_id, created_at);


--
-- Name: idx_agent_session_state_expiry; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX idx_agent_session_state_expiry ON stock_runtime.agent_session_state_items USING btree (expires_at, status);


--
-- Name: idx_agent_session_state_lookup; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX idx_agent_session_state_lookup ON stock_runtime.agent_session_state_items USING btree (session_id, memory_key, status, version DESC);


--
-- Name: idx_memory_records_source; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX idx_memory_records_source ON stock_runtime.memory_records USING btree (source_type, source_id);


--
-- Name: idx_memory_records_user_type_status; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX idx_memory_records_user_type_status ON stock_runtime.memory_records USING btree (user_id, memory_type, status, updated_at);


--
-- Name: idx_task_events_task; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX idx_task_events_task ON stock_runtime.task_events USING btree (task_id, sequence);


--
-- Name: idx_task_runs_lookup; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX idx_task_runs_lookup ON stock_runtime.task_runs USING btree (owner_id, session_id, task_type, created_at DESC);


--
-- Name: idx_task_runs_status; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX idx_task_runs_status ON stock_runtime.task_runs USING btree (status, updated_at DESC);


--
-- Name: memory_records__idx_memory_records_source; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX memory_records__idx_memory_records_source ON stock_runtime.memory_records USING btree (source_type, source_id);


--
-- Name: memory_records__idx_memory_records_user_type_status; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX memory_records__idx_memory_records_user_type_status ON stock_runtime.memory_records USING btree (user_id, memory_type, status, updated_at);


--
-- Name: task_events__idx_task_events_task; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX task_events__idx_task_events_task ON stock_runtime.task_events USING btree (task_id, sequence);


--
-- Name: task_runs__idx_task_runs_lookup; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX task_runs__idx_task_runs_lookup ON stock_runtime.task_runs USING btree (owner_id, session_id, task_type, created_at);


--
-- Name: task_runs__idx_task_runs_status; Type: INDEX; Schema: stock_runtime; Owner: -
--

CREATE INDEX task_runs__idx_task_runs_status ON stock_runtime.task_runs USING btree (status, updated_at);


--
-- PostgreSQL database dump complete
--

\unrestrict YVmZq1DTMXFjl4P9lvGxyAnCozt2t7h2OWiA4H62fXINDtIqN0eKndxJqLOZH9e

