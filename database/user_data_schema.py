"""为个人数据建立独立 PostgreSQL schema；账号、持仓、会话仍保留原表。"""
from __future__ import annotations

import threading
from typing import Any
from core.user_scope import user_schema

# 这些旧表没有 user_id。使用同库独立 schema，避免改写所有新闻/排名 SQL。
# 只复制结构/索引，不复制公共历史数据、不改变已有用户业务记录。
DATA_TABLES = (
    "market_data_daily", "model_prediction", "stock_basic", "stock_alias",
    "news_event", "news_chunk", "news_embedding", "news_stock_mapping",
    "news_mapping_concept_stock_map", "news_mapping_mapping_feedback",
    "news_mapping_news_items", "news_mapping_news_stock_links",
    "news_mapping_stock_alias", "news_mapping_stock_master",
    "rag_retrieval_log", "runtime_data_import_audit", "runtime_state_snapshot",
    "backtest_evaluation",
)
_READY: set[tuple[str, int, str, str, str]] = set()
_LOCK = threading.RLock()


def ensure_user_data_schema(conn: Any, settings: Any, user_id: str) -> str:
    from psycopg import sql
    name = user_schema(user_id)
    key = (settings.host, settings.port, settings.database, settings.app_schema, name)
    with _LOCK:
        if key in _READY:
            return name
        try:
            # 同一用户首次 API 读取和 Worker 启动可能并发，需跨进程锁。
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("dataset:" + name,))
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(name)))
            for table in DATA_TABLES:
                conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} (LIKE {}.{} INCLUDING ALL)").format(
                    sql.Identifier(name), sql.Identifier(table),
                    sql.Identifier(settings.app_schema), sql.Identifier(table),
                ))
            # 读请求关闭连接也不能回滚已经建立的数据域。
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        _READY.add(key)
    return name
