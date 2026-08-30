from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd

import news_data
from database.connection import get_connection
from news_content_fetcher import QUALITY_MIN_CHARS, article_text_quality, fetch_article
from news_db_sync import _event_classification, _is_announcement, _trading_calendar
from database.repositories.news_repository import assign_news_trade_date
from database.postgres_config import PostgresSettings
from rag.chunkers import chunk_announcement, chunk_news


INDEX_NAMES = [
    "沪深300", "中证500", "中证1000", "上证指数", "深证成指", "创业板指", "科创50", "北证50",
]
TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "source", "src", "share_token", "shareid",
}


@dataclass(frozen=True)
class FullTextIngestionReport:
    start_date: str
    end_date: str
    listing_rows: int
    unique_articles: int
    existing_title_only_candidates: int
    existing_title_only_recovered: int
    direct_full_text_articles: int
    url_fetch_attempted: int
    url_fetch_success: int
    dropped_without_full_text: int
    full_text_articles_written: int
    mappings_written: int
    chunks_written: int
    title_only_events_deleted: int
    title_only_chunks_deleted: int
    title_only_mappings_deleted: int
    title_only_embeddings_deleted: int
    structured_chunks_updated: int
    cache_news_rows: int
    cache_announcement_rows: int
    latest_publish_time: str
    failure_reasons: dict[str, int]
    ordinary_news_status: str
    ordinary_news_listing_rows: int
    ordinary_news_full_text_written: int
    announcement_full_text_written: int
    source_diagnostics: dict[str, Any]
    archive_path: str
    database_backend: str
    database_schema: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in {"nan", "none", "nat"}:
        return ""
    return " ".join(text.split())


def _stable_id(prefix: str, *parts: Any) -> str:
    text = "\n".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:20]}"


def _canonical_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return raw
    try:
        parts = urlsplit(raw)
        kept = [
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in TRACKING_QUERY_KEYS
        ]
        query = urlencode(sorted(kept))
        path = re.sub(r"/+$", "", parts.path or "/")
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))
    except Exception:
        return raw


def _article_news_id(row: dict[str, Any]) -> str:
    title = _clean_text(row.get("title"))
    source = _clean_text(row.get("source"))
    publish_time = _clean_text(row.get("publish_time"))
    date = str(row.get("date") or "")[:10]
    canonical_url = _canonical_url(row.get("url"))
    # Deliberately exclude stock_code so one article about multiple stocks keeps one news_id.
    identity = canonical_url or f"{source}|{publish_time or date}|{title}"
    return _stable_id("news", identity, title, source)


def _structured_event_type(title: str, content: str, fallback: str = "") -> str:
    text = f"{title} {content}"
    rules = [
        ("financial_report", ["年报", "半年报", "季报", "财报", "业绩预告", "业绩快报", "营收", "净利润"]),
        ("buyback", ["回购"]),
        ("shareholder_reduce", ["减持"]),
        ("shareholder_increase", ["增持"]),
        ("merger_acquisition", ["并购", "收购", "重组", "重大资产"]),
        ("policy", ["政策", "条例", "办法", "意见", "通知", "监管新规", "规划"]),
        ("product_launch", ["发布新品", "新品发布", "产品发布", "正式发布", "首发", "新品"]),
        ("penalty", ["处罚", "罚款", "监管函", "警示函", "立案调查", "问询函"]),
        ("macro_data", ["GDP", "CPI", "PPI", "PMI", "社融", "M2", "利率", "LPR", "非农", "进出口", "宏观数据"]),
        ("contract", ["中标", "签订合同", "项目合同", "订单"]),
        ("dividend", ["分红", "派息", "权益分派"]),
        ("lawsuit", ["诉讼", "仲裁"]),
        ("financing", ["定增", "增发", "融资", "可转债", "发债"]),
        ("management_change", ["董事长", "总经理", "董秘", "辞任", "聘任"]),
        ("risk", ["风险提示", "退市风险", "异常波动", "违约"]),
    ]
    for event_type, keywords in rules:
        if any(keyword.lower() in text.lower() for keyword in keywords):
            return event_type
    return str(fallback or "other")


def _fetch_listing(
    token: str | None,
    stock_pool: dict[str, str],
    start_date: str,
    end_date: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    status: dict[str, Any] = {
        "tushare_announcement_rows": 0,
        "tushare_news_rows": 0,
        "akshare_announcement_rows": 0,
        "akshare_news_rows": 0,
        "ordinary_news_status": "not_attempted",
        "ordinary_news_diagnostics": {},
    }
    ann_df = pd.DataFrame(columns=news_data.EVENT_COLUMNS)
    news_df = pd.DataFrame(columns=news_data.EVENT_COLUMNS)
    start_compact = pd.to_datetime(start_date).strftime("%Y%m%d")
    end_compact = pd.to_datetime(end_date).strftime("%Y%m%d")

    if token:
        ann_df = news_data.fetch_tushare_announcements(token, stock_pool, start_compact, end_compact)
        news_df = news_data.fetch_tushare_news(token, stock_pool, start_compact, end_compact)
        status["tushare_announcement_rows"] = int(len(ann_df))
        status["tushare_news_rows"] = int(len(news_df))
        if not news_df.empty:
            status["ordinary_news_status"] = "success"
            status["ordinary_news_diagnostics"] = {
                "status": "success",
                "provider": "tushare_news",
                "rows_in_range": int(len(news_df)),
            }

    if news_data.ENABLE_AKSHARE_NEWS_FALLBACK:
        if ann_df.empty:
            ann_df = news_data.fetch_akshare_announcements(stock_pool, start_compact, end_compact)
            status["akshare_announcement_rows"] = int(len(ann_df))
        if news_df.empty:
            news_df, ordinary_diag = news_data.fetch_akshare_stock_news(
                stock_pool,
                start_compact,
                end_compact,
                return_status=True,
            )
            status["akshare_news_rows"] = int(len(news_df))
            status["ordinary_news_status"] = str(ordinary_diag.get("status") or "business_empty")
            status["ordinary_news_diagnostics"] = ordinary_diag

    frames = [frame for frame in (ann_df, news_df) if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=news_data.EVENT_COLUMNS), status

    data = pd.concat(frames, ignore_index=True)
    for col in news_data.EVENT_COLUMNS:
        if col not in data.columns:
            data[col] = ""
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data = data.dropna(subset=["date", "code", "title"]).copy()
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)
    data = data[(data["date"] >= start_dt) & (data["date"] <= end_dt)].copy()
    data["code"] = data["code"].astype(str).str.extract(r"(\d{6})")[0]
    data = data.dropna(subset=["code"]).copy()
    data["code"] = data["code"].astype(str).str.zfill(6)
    data["title"] = data["title"].map(_clean_text)
    data["summary"] = data["summary"].map(_clean_text)
    data["content"] = data["content"].map(_clean_text)
    data["source"] = data["source"].map(_clean_text)
    data["url"] = data["url"].map(_canonical_url)
    data = data[data["title"].ne("")].copy()
    data = data.drop_duplicates(subset=["date", "code", "title", "source", "url"], keep="last")
    return data.reset_index(drop=True), status


def _stock_metadata(
    stock_pool: dict[str, str],
    *,
    token: str | None = None,
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {
        str(code).zfill(6): {
            "stock_code": str(code).zfill(6),
            "stock_name": str(name or ""),
            "full_name": "",
            "industry": "",
            "concepts": [],
        }
        for code, name in (stock_pool or {}).items()
    }
    with get_connection() as conn:
        try:
            rows = conn.execute(
                "SELECT stock_code, stock_name, full_name, industry, concepts FROM stock_basic"
            ).fetchall()
        except Exception:
            rows = []
        try:
            mapping_rows = conn.execute(
                """
                SELECT stock_code,
                       MAX(COALESCE(stock_name, '')) AS stock_name,
                       MAX(COALESCE(industry, '')) AS industry
                  FROM news_stock_mapping
                 WHERE TRIM(COALESCE(stock_code, '')) <> ''
                 GROUP BY stock_code
                """
            ).fetchall()
        except Exception:
            mapping_rows = []

    for row in rows:
        code = str(row["stock_code"] or "").split(".")[0].zfill(6)
        concepts_raw = str(row["concepts"] or "").strip()
        concepts: list[str] = []
        if concepts_raw:
            try:
                loaded = json.loads(concepts_raw)
                if isinstance(loaded, list):
                    concepts = [str(v) for v in loaded if str(v).strip()]
                else:
                    concepts = [v.strip() for v in re.split(r"[,;|、]", concepts_raw) if v.strip()]
            except Exception:
                concepts = [v.strip() for v in re.split(r"[,;|、]", concepts_raw) if v.strip()]
        current = out.get(code) or {
            "stock_code": code, "stock_name": "", "full_name": "", "industry": "", "concepts": []
        }
        current.update({
            "stock_code": code,
            "stock_name": str(row["stock_name"] or current.get("stock_name") or ""),
            "full_name": str(row["full_name"] or current.get("full_name") or ""),
            "industry": str(row["industry"] or current.get("industry") or ""),
            "concepts": concepts or list(current.get("concepts") or []),
        })
        out[code] = current

    for row in mapping_rows:
        code = str(row["stock_code"] or "").split(".")[0].zfill(6)
        current = out.get(code) or {
            "stock_code": code, "stock_name": "", "full_name": "", "industry": "", "concepts": []
        }
        if not str(current.get("stock_name") or "").strip():
            current["stock_name"] = str(row["stock_name"] or "")
        if not str(current.get("industry") or "").strip():
            current["industry"] = str(row["industry"] or "")
        out[code] = current

    # External metadata is fetched once per ingestion run, not once per chunk.
    # Tushare stock_basic is preferred; Eastmoney is the fallback inside news_data.
    codes = sorted(out)
    if codes:
        try:
            external = news_data.fetch_stock_entity_metadata(token, codes)
        except Exception as exc:
            print(f"[News] stock entity metadata enrichment skipped: {exc}")
            external = {}
        for code, row in external.items():
            current = out.get(code) or {
                "stock_code": code, "stock_name": "", "full_name": "", "industry": "", "concepts": []
            }
            for key in ("stock_name", "full_name", "industry"):
                if str(row.get(key) or "").strip():
                    current[key] = str(row.get(key) or "")
            out[code] = current
    return out


def _build_entities(
    *,
    stock_codes: list[str],
    stock_meta: dict[str, dict[str, Any]],
    text: str,
) -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    entities: list[dict[str, Any]] = []
    stock_names: list[str] = []
    industries: list[str] = []
    concepts: list[str] = []
    for code in stock_codes:
        meta = stock_meta.get(code, {})
        name = str(meta.get("stock_name") or "")
        full_name = str(meta.get("full_name") or "")
        industry = str(meta.get("industry") or "")
        entities.append({"type": "stock", "code": code, "name": name, "full_name": full_name})
        if name:
            stock_names.append(name)
        if industry and industry not in industries:
            industries.append(industry)
            entities.append({"type": "industry", "name": industry})
        for concept in meta.get("concepts") or []:
            if concept not in concepts:
                concepts.append(str(concept))
    for index_name in INDEX_NAMES:
        if index_name in text:
            entities.append({"type": "index", "name": index_name})
    # Stable de-duplication.
    seen = set()
    deduped = []
    for entity in entities:
        key = json.dumps(entity, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entity)
    return deduped, list(dict.fromkeys(stock_names)), industries, concepts


def _group_articles(events: pd.DataFrame) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for row in events.to_dict(orient="records"):
        news_id = _article_news_id(row)
        code = str(row.get("code") or "").zfill(6)
        name = _clean_text(row.get("name"))
        current = groups.get(news_id)
        if current is None:
            current = dict(row)
            current["news_id"] = news_id
            current["stock_codes"] = []
            current["stock_names"] = []
            groups[news_id] = current
        if code and code not in current["stock_codes"]:
            current["stock_codes"].append(code)
        if name and name not in current["stock_names"]:
            current["stock_names"].append(name)
        # Keep the richest direct body/summary among duplicate stock rows.
        if len(_clean_text(row.get("content"))) > len(_clean_text(current.get("content"))):
            current["content"] = row.get("content")
        if len(_clean_text(row.get("summary"))) > len(_clean_text(current.get("summary"))):
            current["summary"] = row.get("summary")
        if not _clean_text(current.get("url")) and _clean_text(row.get("url")):
            current["url"] = _canonical_url(row.get("url"))
    return list(groups.values())



def _existing_title_only_articles() -> list[dict[str, Any]]:
    """Return legacy title-only rows as article-level recovery candidates.

    These rows are read before the destructive cleanup gate. URL-backed rows can
    therefore be upgraded to validated full text instead of being discarded.
    """
    with get_connection() as conn:
        events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM news_event
                WHERE COALESCE(content_level, 'title_only') != 'full_text'
                ORDER BY COALESCE(publish_time, trade_date, created_at, '') DESC
                """
            ).fetchall()
        ]
        mappings_by_news: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute(
            """
            SELECT *
            FROM news_stock_mapping
            WHERE news_id IN (
                SELECT news_id FROM news_event
                WHERE COALESCE(content_level, 'title_only') != 'full_text'
            )
            """
        ).fetchall():
            item = dict(row)
            mappings_by_news.setdefault(str(item.get("news_id") or ""), []).append(item)

        chunk_codes_by_news: dict[str, list[str]] = {}
        for row in conn.execute(
            """
            SELECT news_id, stock_code
            FROM news_chunk
            WHERE news_id IN (
                SELECT news_id FROM news_event
                WHERE COALESCE(content_level, 'title_only') != 'full_text'
            )
              AND TRIM(COALESCE(stock_code, '')) <> ''
            """
        ).fetchall():
            news_id = str(row["news_id"] or "")
            code = str(row["stock_code"] or "").split(".")[0].zfill(6)
            if code:
                chunk_codes_by_news.setdefault(news_id, []).append(code)

    grouped: dict[str, dict[str, Any]] = {}
    for event in events:
        legacy_news_id = str(event.get("news_id") or "")
        mappings = mappings_by_news.get(legacy_news_id, [])
        codes = [
            str(item.get("stock_code") or "").split(".")[0].zfill(6)
            for item in mappings
            if str(item.get("stock_code") or "").strip()
        ]
        if not codes:
            codes = chunk_codes_by_news.get(legacy_news_id, [])
        codes = list(dict.fromkeys(code for code in codes if code))
        names = [
            _clean_text(item.get("stock_name"))
            for item in mappings
            if _clean_text(item.get("stock_name"))
        ]
        event_row = {
            "date": str(event.get("trade_date") or event.get("publish_time") or "")[:10],
            "title": _clean_text(event.get("title")),
            "summary": _clean_text(event.get("summary")),
            "content": _clean_text(event.get("content")),
            "source": _clean_text(event.get("source")),
            "url": _canonical_url(event.get("url")),
            "publish_time": _clean_text(event.get("publish_time")),
        }
        article_id = _article_news_id(event_row)
        current = grouped.get(article_id)
        if current is None:
            current = {
                **event_row,
                "news_id": article_id,
                "stock_codes": [],
                "stock_names": [],
                "legacy_news_ids": [],
                "recovery_source": "existing_title_only",
            }
            grouped[article_id] = current
        current["legacy_news_ids"].append(legacy_news_id)
        for code in codes:
            if code not in current["stock_codes"]:
                current["stock_codes"].append(code)
        for name in names:
            if name not in current["stock_names"]:
                current["stock_names"].append(name)
        if len(event_row["content"]) > len(_clean_text(current.get("content"))):
            current["content"] = event_row["content"]
        if len(event_row["summary"]) > len(_clean_text(current.get("summary"))):
            current["summary"] = event_row["summary"]
        if not _clean_text(current.get("url")) and event_row["url"]:
            current["url"] = event_row["url"]
    return list(grouped.values())


def _merge_article_candidates(*collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for collection in collections:
        for article in collection:
            news_id = str(article.get("news_id") or _article_news_id(article))
            current = merged.get(news_id)
            if current is None:
                current = dict(article)
                current["news_id"] = news_id
                current["stock_codes"] = list(article.get("stock_codes") or [])
                current["stock_names"] = list(article.get("stock_names") or [])
                current["legacy_news_ids"] = list(article.get("legacy_news_ids") or [])
                merged[news_id] = current
                continue
            for key in ("stock_codes", "stock_names", "legacy_news_ids"):
                for value in article.get(key) or []:
                    if value not in current[key]:
                        current[key].append(value)
            if len(_clean_text(article.get("content"))) > len(_clean_text(current.get("content"))):
                current["content"] = article.get("content")
            if len(_clean_text(article.get("summary"))) > len(_clean_text(current.get("summary"))):
                current["summary"] = article.get("summary")
            if not _clean_text(current.get("url")) and _clean_text(article.get("url")):
                current["url"] = article.get("url")
            if not _clean_text(current.get("publish_time")) and _clean_text(article.get("publish_time")):
                current["publish_time"] = article.get("publish_time")
            if not str(current.get("date") or "").strip() and str(article.get("date") or "").strip():
                current["date"] = article.get("date")
    return list(merged.values())


def _enrich_articles(
    articles: list[dict[str, Any]],
    *,
    output_dir: str | Path,
    workers: int,
    timeout: float,
    retries: int,
    min_chars: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ready: list[dict[str, Any]] = []
    to_fetch: list[dict[str, Any]] = []
    failures: dict[str, int] = {}
    direct = 0

    for article in articles:
        title = _clean_text(article.get("title"))
        content = _clean_text(article.get("content"))
        ok, _ = article_text_quality(content, title=title, min_chars=min_chars)
        if ok:
            item = dict(article)
            item["content"] = content
            item["content_level"] = "full_text"
            ready.append(item)
            direct += 1
        elif str(article.get("url") or "").lower().startswith(("http://", "https://")):
            to_fetch.append(article)
        else:
            failures["missing_url_or_full_text"] = failures.get("missing_url_or_full_text", 0) + 1

    print(
        f"[News/RAG] full-text candidates: total={len(articles)} direct={direct} url_fetch={len(to_fetch)}",
        flush=True,
    )
    if to_fetch:
        max_workers = max(1, min(int(workers), len(to_fetch)))
        completed = 0
        success = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    fetch_article,
                    article,
                    output_dir=output_dir,
                    timeout=timeout,
                    retries=retries,
                    min_chars=min_chars,
                ): article
                for article in to_fetch
            }
            for future in as_completed(future_map):
                article = future_map[future]
                completed += 1
                try:
                    result = future.result()
                except Exception as exc:
                    reason = f"fetch_exception:{type(exc).__name__}"
                    failures[reason] = failures.get(reason, 0) + 1
                    if completed == len(to_fetch) or completed % 25 == 0:
                        print(
                            f"[News/RAG] full-text progress: {completed}/{len(to_fetch)} success={success} failed={completed-success}",
                            flush=True,
                        )
                    continue
                if result.status == "success":
                    item = dict(article)
                    item["content"] = _clean_text(result.content)
                    item["content_level"] = "full_text"
                    item["raw_file_path"] = result.raw_file_path
                    item["fetch_method"] = result.extraction_method
                    ready.append(item)
                    success += 1
                else:
                    reason = result.reason or result.status
                    failures[reason] = failures.get(reason, 0) + 1
                if completed == len(to_fetch) or completed % 25 == 0:
                    print(
                        f"[News/RAG] full-text progress: {completed}/{len(to_fetch)} success={success} failed={completed-success}",
                        flush=True,
                    )

    return ready, {
        "direct_full_text_articles": direct,
        "url_fetch_attempted": len(to_fetch),
        "url_fetch_success": max(0, len(ready) - direct),
        "failure_reasons": failures,
    }


def _archive_and_purge_title_only(
    *,
    archive_dir: str | Path,
) -> dict[str, Any]:
    archive_root = Path(archive_dir)
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = archive_root / f"title_only_deleted_{stamp}.jsonl"

    with get_connection() as conn:
        events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT * FROM news_event
                WHERE COALESCE(content_level, 'title_only') != 'full_text'
                """
            ).fetchall()
        ]
        news_ids = [str(row.get("news_id") or "") for row in events if str(row.get("news_id") or "")]
        if not news_ids:
            archive_path.write_text("", encoding="utf-8")
            return {
                "events_deleted": 0,
                "chunks_deleted": 0,
                "mappings_deleted": 0,
                "embeddings_deleted": 0,
                "archive_path": str(archive_path),
            }

        placeholders = ",".join("%s" for _ in news_ids)
        chunks = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM news_chunk WHERE news_id IN ({placeholders})",
                news_ids,
            ).fetchall()
        ]
        mappings = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM news_stock_mapping WHERE news_id IN ({placeholders})",
                news_ids,
            ).fetchall()
        ]
        chunk_ids = [str(row.get("chunk_id") or "") for row in chunks if str(row.get("chunk_id") or "")]
        embedding_rows = []
        if chunk_ids:
            chunk_placeholders = ",".join("%s" for _ in chunk_ids)
            embedding_rows = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM news_embedding WHERE chunk_id IN ({chunk_placeholders})",
                    chunk_ids,
                ).fetchall()
            ]

        with archive_path.open("w", encoding="utf-8") as handle:
            mapping_by_news: dict[str, list[dict[str, Any]]] = {}
            for mapping in mappings:
                mapping_by_news.setdefault(str(mapping.get("news_id") or ""), []).append(mapping)
            chunks_by_news: dict[str, list[dict[str, Any]]] = {}
            for chunk in chunks:
                chunks_by_news.setdefault(str(chunk.get("news_id") or ""), []).append(chunk)
            for event in events:
                news_id = str(event.get("news_id") or "")
                payload = {
                    "event": event,
                    "chunks": chunks_by_news.get(news_id, []),
                    "mappings": mapping_by_news.get(news_id, []),
                }
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

        embeddings_deleted = 0
        if chunk_ids:
            chunk_placeholders = ",".join("%s" for _ in chunk_ids)
            cur = conn.execute(
                f"DELETE FROM news_embedding WHERE chunk_id IN ({chunk_placeholders})",
                chunk_ids,
            )
            embeddings_deleted = int(cur.rowcount)
        cur = conn.execute(
            f"DELETE FROM news_stock_mapping WHERE news_id IN ({placeholders})",
            news_ids,
        )
        mappings_deleted = int(cur.rowcount)
        cur = conn.execute(
            f"DELETE FROM news_chunk WHERE news_id IN ({placeholders})",
            news_ids,
        )
        chunks_deleted = int(cur.rowcount)
        cur = conn.execute(
            f"DELETE FROM news_event WHERE news_id IN ({placeholders})",
            news_ids,
        )
        events_deleted = int(cur.rowcount)
        conn.commit()

    return {
        "events_deleted": events_deleted,
        "chunks_deleted": chunks_deleted,
        "mappings_deleted": mappings_deleted,
        "embeddings_deleted": embeddings_deleted,
        "archive_path": str(archive_path),
    }


def _sync_full_text_articles(
    output_dir: str | Path,
    stock_pool: dict[str, str],
    articles: list[dict[str, Any]],
    *,
    token: str | None = None,
) -> dict[str, Any]:
    if not articles:
        return {"events_written": 0, "chunks_written": 0, "mappings_written": 0}

    stock_meta = _stock_metadata(stock_pool, token=token)
    rows_for_calendar = []
    for article in articles:
        for code in article.get("stock_codes") or []:
            rows_for_calendar.append({"date": article.get("date"), "code": code})
    calendar_frame = pd.DataFrame(rows_for_calendar or [{"date": datetime.now(), "code": "000000"}])
    calendar = _trading_calendar(calendar_frame, output_dir=output_dir)

    event_records: list[dict[str, Any]] = []
    chunk_records: list[dict[str, Any]] = []
    mapping_records: list[dict[str, Any]] = []

    for article in articles:
        news_id = str(article.get("news_id") or _article_news_id(article))
        title = _clean_text(article.get("title"))
        summary = _clean_text(article.get("summary"))
        content = _clean_text(article.get("content"))
        if not content:
            continue
        source = _clean_text(article.get("source"))
        url = _canonical_url(article.get("url"))
        publish_dt = pd.to_datetime(article.get("publish_time"), errors="coerce")
        if pd.isna(publish_dt):
            publish_dt = pd.to_datetime(article.get("date"), errors="coerce")
        publish_time = "" if pd.isna(publish_dt) else publish_dt.strftime("%Y-%m-%d %H:%M:%S")
        trade_date = "" if pd.isna(publish_dt) else publish_dt.strftime("%Y-%m-%d")
        if calendar and publish_time:
            try:
                trade_date = assign_news_trade_date(publish_time, calendar)
            except Exception:
                pass

        stock_codes = list(dict.fromkeys(str(code).zfill(6) for code in article.get("stock_codes") or [] if str(code).strip()))
        article_names = list(article.get("stock_names") or [])
        for idx, code in enumerate(stock_codes):
            if idx < len(article_names) and _clean_text(article_names[idx]):
                current = stock_meta.get(code) or {
                    "stock_code": code, "stock_name": "", "full_name": "", "industry": "", "concepts": []
                }
                if not _clean_text(current.get("stock_name")):
                    current["stock_name"] = _clean_text(article_names[idx])
                stock_meta[code] = current
        entities, stock_names, industries, concepts = _build_entities(
            stock_codes=stock_codes,
            stock_meta=stock_meta,
            text=f"{title} {summary} {content}",
        )
        primary_industry = industries[0] if len(industries) == 1 else (";".join(industries[:4]) if industries else "")
        old_type, sentiment, strength, impact_confidence, importance, is_major = _event_classification(title)
        event_type = _structured_event_type(title, content, fallback=old_type)
        direction = "positive" if sentiment == "positive" else "negative" if sentiment == "negative" else "neutral"
        is_announcement = _is_announcement(source)
        retention_level = "hot" if direction != "neutral" or is_announcement else "warm"
        content_hash = _stable_id("hash", title, summary, content, source, url)

        event_records.append(
            {
                "news_id": news_id,
                "title": title,
                "summary": summary,
                "content": content,
                "content_level": "full_text",
                "raw_file_path": str(article.get("raw_file_path") or ""),
                "archive_file_path": "",
                "source": source,
                "publish_time": publish_time,
                "trade_date": trade_date,
                "event_type": event_type,
                "sentiment": sentiment,
                "importance_score": importance,
                "is_announcement": is_announcement,
                "url": url,
                "content_hash": content_hash,
                "retention_level": retention_level,
                "is_major_event": is_major,
                "is_used_by_agent": 0,
                "raw_content_saved": int(bool(article.get("raw_file_path"))),
                "expire_at": "",
            }
        )

        chunk_input = {
            "news_id": news_id,
            "title": title,
            "summary": summary,
            "content": content,
            "content_level": "full_text",
            "source": source,
            "publish_time": publish_time,
            "trade_date": trade_date,
            "stock_codes": stock_codes,
            "industry": primary_industry,
            "event_type": event_type,
            "is_announcement": bool(is_announcement),
            "url": url,
            "importance_score": importance,
            "retention_level": retention_level,
            "entities": entities,
            "metadata": {
                "title": title,
                "stock_codes": stock_codes,
                "stock_names": stock_names,
                "entities": entities,
                "industries": industries,
                "concepts": concepts,
                "event_type": event_type,
                "publish_time": publish_time,
                "source": source,
            },
        }
        chunks = chunk_announcement(chunk_input) if is_announcement else chunk_news(chunk_input)
        for chunk in chunks:
            record = chunk.to_database_record()
            record.update({"used_in_decision": 0, "retrieval_count": 0, "expire_at": ""})
            chunk_records.append(record)

        for code in stock_codes:
            meta = stock_meta.get(code, {})
            mapping_records.append(
                {
                    "mapping_id": _stable_id("mapping", news_id, code, "fulltext_ingestion"),
                    "news_id": news_id,
                    "stock_code": code,
                    "stock_name": str(meta.get("stock_name") or ""),
                    "industry": str(meta.get("industry") or ""),
                    "concept": "|".join(meta.get("concepts") or []),
                    "relevance_score": 1.0 if is_announcement else 0.80,
                    "impact_direction": direction,
                    "impact_strength": strength,
                    "impact_confidence": impact_confidence,
                    "mapping_confidence": 0.90 if is_announcement else 0.75,
                    "mapping_method": "fulltext_ingestion",
                    "evidence_text": summary or content[:500],
                }
            )

    with get_connection() as conn:
        # Replace only the articles participating in this ingestion. Existing full-text
        # historical rows outside the refresh window remain untouched.
        news_ids = [record["news_id"] for record in event_records]
        if news_ids:
            placeholders = ",".join("%s" for _ in news_ids)
            old_chunk_ids = [
                str(row["chunk_id"])
                for row in conn.execute(
                    f"SELECT chunk_id FROM news_chunk WHERE news_id IN ({placeholders})",
                    news_ids,
                ).fetchall()
            ]
            if old_chunk_ids:
                cp = ",".join("%s" for _ in old_chunk_ids)
                conn.execute(f"DELETE FROM news_embedding WHERE chunk_id IN ({cp})", old_chunk_ids)
            conn.execute(f"DELETE FROM news_chunk WHERE news_id IN ({placeholders})", news_ids)
            conn.execute(f"DELETE FROM news_stock_mapping WHERE news_id IN ({placeholders})", news_ids)

        def upsert(table: str, records: list[dict[str, Any]], pk: str) -> None:
            if not records:
                return
            columns = list(records[0])
            column_sql = ", ".join(f'"{col}"' for col in columns)
            placeholders_sql = ", ".join(f"%({col})s" for col in columns)
            update_cols = [col for col in columns if col != pk]
            update_sql = ", ".join(f'"{col}"=EXCLUDED."{col}"' for col in update_cols)
            sql = (
                f'INSERT INTO "{table}" ({column_sql}) VALUES ({placeholders_sql}) '
                f'ON CONFLICT ("{pk}") DO UPDATE SET {update_sql}'
            )
            with conn.cursor() as cur:
                cur.executemany(sql, records)

        upsert("news_event", event_records, "news_id")
        upsert("news_chunk", chunk_records, "chunk_id")
        upsert("news_stock_mapping", mapping_records, "mapping_id")
        conn.commit()

    return {
        "events_written": len(event_records),
        "chunks_written": len(chunk_records),
        "mappings_written": len(mapping_records),
    }


def backfill_structured_chunk_metadata(*, token: str | None = None) -> int:
    stock_meta = _stock_metadata({}, token=token)
    with get_connection() as conn:
        events = {
            str(row["news_id"]): dict(row)
            for row in conn.execute("SELECT * FROM news_event WHERE content_level='full_text'").fetchall()
        }
        mappings_by_news: dict[str, list[dict[str, Any]]] = {}
        for row in conn.execute("SELECT * FROM news_stock_mapping").fetchall():
            item = dict(row)
            mappings_by_news.setdefault(str(item.get("news_id") or ""), []).append(item)

        updates: list[dict[str, Any]] = []
        for row in conn.execute("SELECT * FROM news_chunk WHERE content_level='full_text'").fetchall():
            chunk = dict(row)
            news_id = str(chunk.get("news_id") or "")
            event = events.get(news_id, {})
            mappings = mappings_by_news.get(news_id, [])
            codes = [
                str(mapping.get("stock_code") or "").zfill(6)
                for mapping in mappings
                if str(mapping.get("stock_code") or "").strip()
            ]
            if not codes and str(chunk.get("stock_code") or "").strip():
                codes = [str(chunk.get("stock_code") or "").zfill(6)]
            codes = list(dict.fromkeys(codes))
            entities, stock_names, industries, concepts = _build_entities(
                stock_codes=codes,
                stock_meta=stock_meta,
                text=f"{event.get('title','')} {event.get('content','')}",
            )
            title = _clean_text(event.get("title") or chunk.get("section_title"))
            event_type = _structured_event_type(
                title,
                _clean_text(event.get("content")),
                fallback=str(event.get("event_type") or chunk.get("event_type") or "other"),
            )
            primary_industry = industries[0] if len(industries) == 1 else (
                ";".join(industries[:4]) if industries else str(chunk.get("industry") or "")
            )
            metadata = {
                "title": title,
                "stock_codes": codes,
                "stock_names": stock_names,
                "entities": entities,
                "industries": industries,
                "concepts": concepts,
                "event_type": event_type,
                "publish_time": str(event.get("publish_time") or chunk.get("publish_time") or ""),
                "source": str(event.get("source") or chunk.get("source") or ""),
            }
            updates.append(
                {
                    "chunk_id": str(chunk.get("chunk_id") or ""),
                    "title": title,
                    "stock_code": codes[0] if codes else str(chunk.get("stock_code") or ""),
                    "stock_codes_json": json.dumps(codes, ensure_ascii=False),
                    "entities_json": json.dumps(entities, ensure_ascii=False),
                    "metadata_json": json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    "industry": primary_industry,
                    "event_type": event_type,
                }
            )

        if updates:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    UPDATE news_chunk
                       SET title=%(title)s,
                           stock_code=%(stock_code)s,
                           stock_codes_json=%(stock_codes_json)s,
                           entities_json=%(entities_json)s,
                           metadata_json=%(metadata_json)s,
                           industry=%(industry)s,
                           event_type=%(event_type)s
                     WHERE chunk_id=%(chunk_id)s
                    """,
                    updates,
                )
            conn.commit()
    return len(updates)


def _rewrite_full_text_caches(
    enriched_articles: list[dict[str, Any]],
    *,
    archive_dir: str | Path,
) -> tuple[int, int]:
    archive_root = Path(archive_dir) / "cache_snapshots"
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for cache_path in [Path(news_data.NEWS_CACHE_PATH), Path(news_data.ANNOUNCEMENT_CACHE_PATH)]:
        if cache_path.exists():
            shutil.copy2(cache_path, archive_root / f"{cache_path.stem}_{stamp}{cache_path.suffix}")

    news_rows: list[dict[str, Any]] = []
    ann_rows: list[dict[str, Any]] = []
    for article in enriched_articles:
        for code in article.get("stock_codes") or []:
            row = {
                "date": pd.to_datetime(article.get("date"), errors="coerce").strftime("%Y-%m-%d"),
                "code": code,
                "name": "",
                "title": _clean_text(article.get("title")),
                "summary": _clean_text(article.get("summary")),
                "content": _clean_text(article.get("content")),
                "source": _clean_text(article.get("source")),
                "url": _canonical_url(article.get("url")),
                "publish_time": _clean_text(article.get("publish_time")),
            }
            if _is_announcement(row["source"]):
                ann_rows.append(row)
            else:
                news_rows.append(row)

    def write(path: Path, rows: list[dict[str, Any]]) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = pd.DataFrame(rows, columns=news_data.EVENT_COLUMNS)
        if not frame.empty:
            frame = frame.drop_duplicates(subset=["date", "code", "title", "source", "url"], keep="last")
            frame = frame.sort_values(["date", "code", "publish_time"])
        frame.to_csv(path, index=False, encoding="utf-8-sig")
        return int(len(frame))

    return write(Path(news_data.NEWS_CACHE_PATH), news_rows), write(Path(news_data.ANNOUNCEMENT_CACHE_PATH), ann_rows)


def run_full_text_news_ingestion(
    *,
    token: str | None,
    stock_pool: dict[str, str],
    start_date: str,
    end_date: str,
    output_dir: str | Path = "outputs",
    workers: int = 6,
    timeout: float = 15.0,
    retries: int = 1,
    min_chars: int = QUALITY_MIN_CHARS,
    purge_title_only: bool = True,
) -> FullTextIngestionReport:
    listing, fetch_status = _fetch_listing(token, stock_pool, start_date, end_date)
    fresh_articles = _group_articles(listing)
    fresh_ordinary_article_ids = {
        str(item.get("news_id") or "")
        for item in fresh_articles
        if not _is_announcement(str(item.get("source") or ""))
    }
    fresh_announcement_article_ids = {
        str(item.get("news_id") or "")
        for item in fresh_articles
        if _is_announcement(str(item.get("source") or ""))
    }
    legacy_title_only_articles = _existing_title_only_articles()
    articles = _merge_article_candidates(fresh_articles, legacy_title_only_articles)
    legacy_candidate_ids = {
        str(item.get("news_id") or "")
        for item in legacy_title_only_articles
        if str(item.get("news_id") or "")
    }
    full_text_output = Path(output_dir) / "news_full_text"
    enriched, enrich_status = _enrich_articles(
        articles,
        output_dir=full_text_output,
        workers=workers,
        timeout=timeout,
        retries=retries,
        min_chars=min_chars,
    )

    # Safety gates: a daily refresh must prove the upstream listing path worked,
    # and destructive cleanup is forbidden when no validated full text was acquired.
    if len(listing) == 0:
        raise RuntimeError(
            "news_listing_business_empty: no current news/announcement rows were returned; "
            "title-only purge was not executed"
        )
    if len(articles) > 0 and len(enriched) == 0:
        raise RuntimeError(
            "full_text_ingestion_failed: candidates existed but no validated full text was acquired; "
            "title-only purge was not executed"
        )

    cleanup_root = Path(output_dir) / "news_cleanup"
    purge_result = {
        "events_deleted": 0,
        "chunks_deleted": 0,
        "mappings_deleted": 0,
        "embeddings_deleted": 0,
        "archive_path": "",
    }
    if purge_title_only:
        purge_result = _archive_and_purge_title_only(archive_dir=cleanup_root)

    write_result = _sync_full_text_articles(output_dir, stock_pool, enriched, token=token)
    structured_updated = backfill_structured_chunk_metadata(token=token)
    cache_news_rows, cache_announcement_rows = _rewrite_full_text_caches(
        enriched,
        archive_dir=cleanup_root,
    )

    latest_publish = ""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(publish_time) AS latest FROM news_event WHERE content_level='full_text'"
        ).fetchone()
        latest_publish = str(row["latest"] if row else "")

    recovered_legacy = sum(
        1
        for item in enriched
        if str(item.get("news_id") or "") in legacy_candidate_ids
    )
    ordinary_full_text_written = sum(
        1 for item in enriched if str(item.get("news_id") or "") in fresh_ordinary_article_ids
    )
    announcement_full_text_written = sum(
        1 for item in enriched if str(item.get("news_id") or "") in fresh_announcement_article_ids
    )
    ordinary_news_status = str(fetch_status.get("ordinary_news_status") or "not_attempted")
    ordinary_diag = dict(fetch_status.get("ordinary_news_diagnostics") or {})
    failure_reasons = dict(enrich_status.get("failure_reasons") or {})
    failure_reasons.update({
        f"source:{key}": int(value)
        for key, value in fetch_status.items()
        if key.endswith("_rows") and int(value or 0) == 0
    })
    report = FullTextIngestionReport(
        start_date=start_date,
        end_date=end_date,
        listing_rows=int(len(listing)),
        unique_articles=int(len(articles)),
        existing_title_only_candidates=int(len(legacy_title_only_articles)),
        existing_title_only_recovered=int(recovered_legacy),
        direct_full_text_articles=int(enrich_status.get("direct_full_text_articles") or 0),
        url_fetch_attempted=int(enrich_status.get("url_fetch_attempted") or 0),
        url_fetch_success=int(enrich_status.get("url_fetch_success") or 0),
        dropped_without_full_text=max(0, int(len(articles) - len(enriched))),
        full_text_articles_written=int(write_result["events_written"]),
        mappings_written=int(write_result["mappings_written"]),
        chunks_written=int(write_result["chunks_written"]),
        title_only_events_deleted=int(purge_result["events_deleted"]),
        title_only_chunks_deleted=int(purge_result["chunks_deleted"]),
        title_only_mappings_deleted=int(purge_result["mappings_deleted"]),
        title_only_embeddings_deleted=int(purge_result["embeddings_deleted"]),
        structured_chunks_updated=int(structured_updated),
        cache_news_rows=int(cache_news_rows),
        cache_announcement_rows=int(cache_announcement_rows),
        latest_publish_time=latest_publish,
        failure_reasons=failure_reasons,
        ordinary_news_status=ordinary_news_status,
        ordinary_news_listing_rows=int(fetch_status.get("tushare_news_rows") or fetch_status.get("akshare_news_rows") or 0),
        ordinary_news_full_text_written=int(ordinary_full_text_written),
        announcement_full_text_written=int(announcement_full_text_written),
        source_diagnostics={
            "tushare_announcement_rows": int(fetch_status.get("tushare_announcement_rows") or 0),
            "tushare_news_rows": int(fetch_status.get("tushare_news_rows") or 0),
            "akshare_announcement_rows": int(fetch_status.get("akshare_announcement_rows") or 0),
            "akshare_news_rows": int(fetch_status.get("akshare_news_rows") or 0),
            "ordinary_news": ordinary_diag,
        },
        archive_path=str(purge_result.get("archive_path") or ""),
        database_backend="postgresql",
        database_schema=PostgresSettings.from_env().app_schema,
    )
    report_dir = Path(output_dir) / "news_full_text"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "last_fulltext_ingestion_report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report
