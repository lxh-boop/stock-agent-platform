from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

from database.postgres_store import PostgresStore
from news_db_sync import classify_content_level
from rag.chunkers import chunk_announcement, chunk_news


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
QUALITY_MIN_CHARS = 80
FETCH_SCHEMA_VERSION = 1


def _clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _stable_id(prefix: str, *parts: Any) -> str:
    text = "\n".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(text.encode('utf-8')).hexdigest()[:20]}"


def _content_hash(title: str, summary: str, content: str, source: str, url: str) -> str:
    return _stable_id("hash", title, summary, content, source, url)


class _ArticleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._target_depth = 0
        self._skip_depth = 0
        self._p_depth = 0
        self.target_parts: list[str] = []
        self.paragraph_parts: list[str] = []
        self.meta_description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {str(k).lower(): str(v or "") for k, v in attrs}
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            return
        if attrs_map.get("style", "").replace(" ", "").lower().find("display:none") >= 0:
            self._skip_depth += 1
            return
        if tag == "meta" and attrs_map.get("name", "").lower() == "description":
            self.meta_description = _clean_text(attrs_map.get("content", ""))
            return
        if self._is_target_container(attrs_map):
            self._target_depth = 1
        elif self._target_depth > 0:
            self._target_depth += 1
        if tag == "p":
            self._p_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth > 0 and tag in {"script", "style", "noscript", "svg", "p", "div"}:
            self._skip_depth -= 1
            return
        if self._target_depth > 0:
            self._target_depth -= 1
        if tag == "p" and self._p_depth > 0:
            self._p_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        text = _clean_text(data)
        if not text:
            return
        if self._target_depth > 0:
            self.target_parts.append(text)
        if self._p_depth > 0:
            self.paragraph_parts.append(text)

    @staticmethod
    def _is_target_container(attrs: dict[str, str]) -> bool:
        node_id = attrs.get("id", "").lower()
        classes = attrs.get("class", "").lower()
        if node_id in {"contentbody", "articlecontent", "article", "newscontent"}:
            return True
        return any(
            token in classes
            for token in [
                "txtinfos",
                "article-content",
                "article_content",
                "news-content",
                "post-content",
                "contentbody",
            ]
        )

    def candidate_texts(self) -> list[tuple[str, str]]:
        candidates = [
            ("target_container", " ".join(self.target_parts)),
            ("paragraphs", " ".join(self.paragraph_parts)),
            ("meta_description", self.meta_description),
        ]
        return [(method, _clean_text(text)) for method, text in candidates if _clean_text(text)]


@dataclass(frozen=True)
class FetchResult:
    news_id: str
    url: str
    status: str
    content: str = ""
    extraction_method: str = ""
    http_status: int = 0
    reason: str = ""
    content_hash: str = ""
    raw_file_path: str = ""
    fetched_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BackfillReport:
    scanned: int
    attempted: int
    success: int
    failed: int
    skipped: int
    chunk_rows_written: int
    output_dir: str
    failure_reasons: dict[str, int]
    samples: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract_article_text(html_text: str, *, title: str = "", min_chars: int = QUALITY_MIN_CHARS) -> tuple[str, str, str]:
    parser = _ArticleTextParser()
    parser.feed(html_text or "")
    for method, text in parser.candidate_texts():
        ok, reason = article_text_quality(text, title=title, min_chars=min_chars)
        if ok:
            return text, method, ""
    best = parser.candidate_texts()[0][1] if parser.candidate_texts() else ""
    _, reason = article_text_quality(best, title=title, min_chars=min_chars)
    return "", "", reason


def article_text_quality(text: str, *, title: str = "", min_chars: int = QUALITY_MIN_CHARS) -> tuple[bool, str]:
    cleaned = _clean_text(text)
    title_text = _clean_text(title)
    if not cleaned:
        return False, "empty_text"
    if cleaned == title_text:
        return False, "same_as_title"
    if len(cleaned) < max(int(min_chars), len(title_text) + 10):
        return False, "too_short"
    bad_markers = ["access denied", "forbidden", "captcha", "验证码", "登录后", "请开启javascript"]
    lower = cleaned.lower()
    if any(marker in lower for marker in bad_markers):
        return False, "blocked_or_login_page"
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", cleaned))
    if chinese_count < 20 and chinese_count / max(1, len(cleaned)) < 0.15:
        return False, "not_article_like"
    return True, ""


def fetch_article(
    event: dict[str, Any],
    *,
    output_dir: str | Path = "outputs/news_full_text",
    timeout: float = 12.0,
    retries: int = 2,
    min_chars: int = QUALITY_MIN_CHARS,
) -> FetchResult:
    news_id = str(event.get("news_id") or "")
    url = str(event.get("url") or "").strip()
    title = str(event.get("title") or "")
    if not news_id:
        return FetchResult(news_id="", url=url, status="skipped", reason="missing_news_id")
    if not url.lower().startswith(("http://", "https://")):
        return FetchResult(news_id=news_id, url=url, status="skipped", reason="missing_url")

    last_reason = ""
    http_status = 0
    for attempt in range(max(1, int(retries))):
        try:
            response = requests.get(
                url,
                timeout=float(timeout),
                headers={"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9"},
                allow_redirects=True,
            )
            http_status = int(response.status_code)
            if response.status_code >= 400:
                last_reason = f"http_{response.status_code}"
            else:
                content, method, reason = extract_article_text(
                    response.text,
                    title=title,
                    min_chars=min_chars,
                )
                if content:
                    raw_path = _write_raw_html(output_dir, news_id, response.text)
                    return FetchResult(
                        news_id=news_id,
                        url=response.url or url,
                        status="success",
                        content=content,
                        extraction_method=method,
                        http_status=http_status,
                        content_hash=hashlib.sha1(content.encode("utf-8")).hexdigest(),
                        raw_file_path=str(raw_path),
                        fetched_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                last_reason = reason or "quality_check_failed"
        except requests.RequestException as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < max(1, int(retries)):
            time.sleep(min(2.0, 0.25 * (attempt + 1)))

    return FetchResult(news_id=news_id, url=url, status="failed", http_status=http_status, reason=last_reason)


def _write_raw_html(output_dir: str | Path, news_id: str, html_text: str) -> Path:
    root = Path(output_dir) / "raw_html"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{news_id}.html"
    path.write_text(html_text, encoding="utf-8")
    return path


def _select_candidates(db_path: str | Path, *, limit: int = 50) -> list[dict[str, Any]]:
    _ = db_path
    store = PostgresStore()
    rows = store.fetch_all(
                """
                SELECT *
                FROM news_event
                WHERE COALESCE(content_level, 'title_only') != 'full_text'
                  AND COALESCE(url, '') LIKE 'http%'
                ORDER BY COALESCE(publish_time, created_at, '') DESC
                """
    )
    if limit <= 0 or len(rows) <= limit:
        return rows
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_source.setdefault(str(row.get("source") or ""), []).append(row)
    selected: list[dict[str, Any]] = []
    while len(selected) < limit and by_source:
        for source in list(by_source):
            if not by_source[source]:
                by_source.pop(source, None)
                continue
            selected.append(by_source[source].pop(0))
            if len(selected) >= limit:
                break
    return selected


def _existing_stock_code(conn, news_id: str) -> str:
    row = conn.execute(
        "SELECT stock_code FROM news_chunk WHERE news_id = %s AND COALESCE(stock_code, '') != '' LIMIT 1",
        (news_id,),
    ).fetchone()
    return str(row["stock_code"] if row else "")


def _upsert_chunks(conn, records: list[dict[str, Any]]) -> None:
    if not records:
        return
    store = PostgresStore()
    for record in records:
        store.upsert("news_chunk", record, connection=conn)


def _apply_success(conn, event: dict[str, Any], result: FetchResult) -> int:
    news_id = str(event.get("news_id") or "")
    current_content = _clean_text(event.get("content"))
    current_level = str(event.get("content_level") or "title_only")
    if current_level == "full_text" and len(current_content) >= len(result.content):
        return 0

    title = _clean_text(event.get("title"))
    summary = _clean_text(event.get("summary"))
    source = str(event.get("source") or "")
    url = str(event.get("url") or result.url)
    stock_code = _existing_stock_code(conn, news_id)
    content_level = classify_content_level(title, summary, result.content)
    if content_level != "full_text":
        return 0

    conn.execute(
        """
        UPDATE news_event
           SET content = %s,
               content_level = 'full_text',
               content_hash = %s,
               raw_content_saved = 1,
               raw_file_path = %s
         WHERE news_id = %s
        """,
        (_clean_text(result.content), _content_hash(title, summary, result.content, source, url), result.raw_file_path, news_id),
    )
    conn.execute("DELETE FROM news_chunk WHERE news_id = %s", (news_id,))
    chunk_input = {
        "news_id": news_id,
        "title": title,
        "summary": summary,
        "content": result.content,
        "content_level": "full_text",
        "source": source,
        "publish_time": event.get("publish_time") or "",
        "trade_date": event.get("trade_date") or "",
        "stock_codes": [stock_code] if stock_code else [],
        "stock_code": stock_code,
        "industry": "",
        "event_type": event.get("event_type") or "",
        "is_announcement": bool(event.get("is_announcement")),
        "url": url,
        "importance_score": event.get("importance_score"),
        "retention_level": event.get("retention_level") or "hot",
    }
    chunks = chunk_announcement(chunk_input) if bool(event.get("is_announcement")) else chunk_news(chunk_input)
    chunk_records = []
    for chunk in chunks:
        record = chunk.to_database_record()
        record.update({"used_in_decision": 0, "retrieval_count": 0, "expire_at": ""})
        chunk_records.append(record)
    _upsert_chunks(conn, chunk_records)
    return len(chunk_records)


def backfill_news_full_text(
    *,
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs/news_full_text",
    limit: int = 50,
    workers: int = 4,
    timeout: float = 12.0,
    retries: int = 2,
    min_chars: int = QUALITY_MIN_CHARS,
    dry_run: bool = False,
) -> BackfillReport:
    candidates = _select_candidates(db_path, limit=limit)
    results: list[FetchResult] = []
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), max(1, len(candidates))))) as executor:
        future_map = {
            executor.submit(
                fetch_article,
                row,
                output_dir=output_dir,
                timeout=timeout,
                retries=retries,
                min_chars=min_chars,
            ): row
            for row in candidates
        }
        for future in as_completed(future_map):
            results.append(future.result())

    chunk_rows = 0
    if not dry_run:
        _ = db_path
        by_id = {str(row.get("news_id") or ""): row for row in candidates}
        with PostgresStore().transaction() as conn:
            for result in results:
                if result.status == "success":
                    chunk_rows += _apply_success(conn, by_id.get(result.news_id, {}), result)

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    fail_path = output_root / "fetch_failures.jsonl"
    with fail_path.open("a", encoding="utf-8") as file:
        for result in results:
            if result.status != "success":
                file.write(json.dumps(result.to_dict(), ensure_ascii=False) + "\n")

    reason_counts: dict[str, int] = {}
    for result in results:
        if result.status != "success":
            reason_counts[result.reason or result.status] = reason_counts.get(result.reason or result.status, 0) + 1

    success = sum(1 for result in results if result.status == "success")
    skipped = sum(1 for result in results if result.status == "skipped")
    failed = len(results) - success - skipped
    report = BackfillReport(
        scanned=len(candidates),
        attempted=len(results),
        success=success,
        failed=failed,
        skipped=skipped,
        chunk_rows_written=chunk_rows,
        output_dir=str(output_root),
        failure_reasons=reason_counts,
        samples=[
            {
                "news_id": result.news_id,
                "status": result.status,
                "method": result.extraction_method,
                "chars": len(result.content),
                "reason": result.reason,
            }
            for result in results[:10]
        ],
    )
    (output_root / "last_backfill_report.json").write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill real article text for URL-backed news events.")
    parser.add_argument("--db-path", default="")
    parser.add_argument("--output-dir", default="outputs/news_full_text")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--min-chars", type=int, default=QUALITY_MIN_CHARS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report-path", default="")
    args = parser.parse_args(argv)
    report = backfill_news_full_text(
        db_path=args.db_path,
        output_dir=args.output_dir,
        limit=args.limit,
        workers=args.workers,
        timeout=args.timeout,
        retries=args.retries,
        min_chars=args.min_chars,
        dry_run=args.dry_run,
    )
    text = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    if args.report_path:
        path = Path(args.report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text)
    return 0 if report.success > 0 or report.attempted == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
