from __future__ import annotations

from core.user_scope import owned_path

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote

import pandas as pd
import requests

import config as data_config
from data_tushare import init_tushare_pro, ts_code_to_code

NEWS_CACHE_PATH = getattr(data_config, "NEWS_CACHE_PATH", os.path.join("data", "news_cache.csv"))
ANNOUNCEMENT_CACHE_PATH = getattr(
    data_config,
    "ANNOUNCEMENT_CACHE_PATH",
    os.path.join("data", "announcement_cache.csv"),
)
ENABLE_AKSHARE_NEWS_FALLBACK = getattr(data_config, "ENABLE_AKSHARE_NEWS_FALLBACK", True)
AKSHARE_FETCH_ANNOUNCEMENTS = getattr(data_config, "AKSHARE_FETCH_ANNOUNCEMENTS", True)
AKSHARE_FETCH_STOCK_NEWS = getattr(data_config, "AKSHARE_FETCH_STOCK_NEWS", True)
AKSHARE_NOTICE_RECENT_PAGES = int(getattr(data_config, "AKSHARE_NOTICE_RECENT_PAGES", 20))
AKSHARE_NOTICE_MAX_DAYS = int(getattr(data_config, "AKSHARE_NOTICE_MAX_DAYS", 10))
AKSHARE_STOCK_NEWS_MAX_CODES = int(getattr(data_config, "AKSHARE_STOCK_NEWS_MAX_CODES", 300))
AKSHARE_REQUEST_SLEEP_SECONDS = float(getattr(data_config, "AKSHARE_REQUEST_SLEEP_SECONDS", 0.05))
AKSHARE_FETCH_WORKERS = int(getattr(data_config, "AKSHARE_FETCH_WORKERS", 4))
EASTMONEY_STOCK_NEWS_MAX_PAGES = int(getattr(data_config, "EASTMONEY_STOCK_NEWS_MAX_PAGES", 3))
EASTMONEY_STOCK_NEWS_PAGE_SIZE = int(getattr(data_config, "EASTMONEY_STOCK_NEWS_PAGE_SIZE", 100))
EASTMONEY_STOCK_NEWS_TIMEOUT_SECONDS = float(getattr(data_config, "EASTMONEY_STOCK_NEWS_TIMEOUT_SECONDS", 12.0))



_STOCK_ENTITY_METADATA_RUNTIME_CACHE: dict[str, dict[str, str]] = {}

EVENT_COLUMNS = [
    "date",
    "code",
    "name",
    "title",
    "summary",
    "content",
    "source",
    "url",
    "publish_time",
]


def _first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for col in candidates:
        if col in df.columns:
            return col
    return None


def _date_in_range(data: pd.DataFrame, start_date: str, end_date: str) -> pd.DataFrame:
    if data.empty or "date" not in data.columns:
        return data
    start = pd.to_datetime(start_date, errors="coerce")
    end = pd.to_datetime(end_date, errors="coerce")
    if pd.isna(start) or pd.isna(end):
        return data
    out = data.copy()
    dates = pd.to_datetime(out["date"], errors="coerce")
    out = out[(dates >= start) & (dates <= end)].copy()
    return out.reset_index(drop=True)


def _import_akshare():
    try:
        import akshare as ak  # type: ignore

        return ak
    except Exception as e:
        print(f"[News] AkShare unavailable, skipped: {e}")
        return None


def _resolve_akshare_fetch_workers(task_count: int) -> int:
    if task_count <= 0:
        return 1

    worker_value = os.environ.get("AKSHARE_FETCH_WORKERS") or os.environ.get("NEWS_FETCH_WORKERS")
    workers = AKSHARE_FETCH_WORKERS
    if worker_value:
        try:
            workers = int(float(worker_value))
        except ValueError:
            workers = AKSHARE_FETCH_WORKERS

    return max(1, min(int(workers), task_count))


def _sleep_after_akshare_request() -> None:
    if AKSHARE_REQUEST_SLEEP_SECONDS > 0:
        time.sleep(AKSHARE_REQUEST_SLEEP_SECONDS)


def _fetch_akshare_announcement_dates(ak, start_dt, end_dt) -> pd.DataFrame:
    date_values = list(pd.date_range(start=start_dt, end=end_dt, freq="D"))
    worker_count = _resolve_akshare_fetch_workers(len(date_values))
    frames_by_index: dict[int, pd.DataFrame] = {}

    def fetch_one(index: int, dt) -> tuple[int, pd.DataFrame | None, str | None]:
        date_text = dt.strftime("%Y%m%d")
        try:
            daily = ak.stock_notice_report(symbol="\u5168\u90e8", date=date_text)
            return index, daily, None
        except Exception as e:
            return index, None, f"[News] AkShare announcement date skipped for {date_text}: {e}"
        finally:
            _sleep_after_akshare_request()

    if worker_count <= 1:
        rows = [fetch_one(index, dt) for index, dt in enumerate(date_values)]
    else:
        rows = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(fetch_one, index, dt): index
                for index, dt in enumerate(date_values)
            }
            for future in as_completed(future_map):
                rows.append(future.result())

    for index, daily, error in rows:
        if error:
            print(error)
            continue
        if daily is not None and not daily.empty:
            frames_by_index[index] = daily

    frames = [frames_by_index[index] for index in range(len(date_values)) if index in frames_by_index]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _fetch_akshare_stock_news_frames(
    ak,
    stock_pool: dict,
    codes: list[str],
    *,
    start_date: str = "",
    end_date: str = "",
) -> tuple[list[pd.DataFrame], dict[str, object]]:
    worker_count = _resolve_akshare_fetch_workers(len(codes))
    diagnostics: dict[str, object] = {
        "codes_attempted": len(codes),
        "codes_with_rows": 0,
        "codes_business_empty": 0,
        "codes_provider_failed": 0,
        "rows_raw": 0,
        "rows_in_range": 0,
        "akshare_primary_success_codes": 0,
        "eastmoney_fallback_success_codes": 0,
        "eastmoney_name_fallback_success_codes": 0,
        "error_samples": [],
    }

    def fetch_one(index: int, code: str) -> tuple[int, pd.DataFrame | None, dict[str, object]]:
        normalized_code = str(code).zfill(6)
        stock_name = str(stock_pool.get(normalized_code, "") or "")
        try:
            raw, status = _call_akshare_stock_news(
                ak,
                normalized_code,
                stock_name=stock_name,
                start_date=start_date,
                end_date=end_date,
            )
            if raw is None or raw.empty:
                return index, None, status
            data = raw.copy()
            data["code"] = normalized_code
            data["name"] = stock_name
            return index, data, status
        except Exception as e:
            return index, None, {
                "status": "provider_failed",
                "provider": "akshare_eastmoney",
                "error": f"{type(e).__name__}: {e}",
                "raw_rows": 0,
                "in_range_rows": 0,
            }
        finally:
            _sleep_after_akshare_request()

    if worker_count <= 1:
        rows = [fetch_one(index, code) for index, code in enumerate(codes)]
    else:
        rows = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(fetch_one, index, code): index
                for index, code in enumerate(codes)
            }
            completed = 0
            for future in as_completed(future_map):
                rows.append(future.result())
                completed += 1
                if completed == len(codes) or completed % 25 == 0:
                    print(f"[News] ordinary stock news progress: {completed}/{len(codes)} codes", flush=True)

    frames_by_index: dict[int, pd.DataFrame] = {}
    error_samples: list[str] = []
    for index, data, status in rows:
        diagnostics["rows_raw"] = int(diagnostics["rows_raw"]) + int(status.get("raw_rows") or 0)
        diagnostics["rows_in_range"] = int(diagnostics["rows_in_range"]) + int(status.get("in_range_rows") or 0)
        provider = str(status.get("provider") or "")
        if provider == "akshare":
            diagnostics["akshare_primary_success_codes"] = int(diagnostics["akshare_primary_success_codes"]) + 1
        elif provider == "eastmoney_direct":
            diagnostics["eastmoney_fallback_success_codes"] = int(diagnostics["eastmoney_fallback_success_codes"]) + 1
        elif provider == "eastmoney_name":
            diagnostics["eastmoney_name_fallback_success_codes"] = int(diagnostics["eastmoney_name_fallback_success_codes"]) + 1

        code_status = str(status.get("status") or "")
        if code_status == "provider_failed":
            diagnostics["codes_provider_failed"] = int(diagnostics["codes_provider_failed"]) + 1
            error = str(status.get("error") or "").strip()
            if error and len(error_samples) < 8:
                error_samples.append(error[:500])
            continue
        if data is None or data.empty:
            diagnostics["codes_business_empty"] = int(diagnostics["codes_business_empty"]) + 1
            continue
        diagnostics["codes_with_rows"] = int(diagnostics["codes_with_rows"]) + 1
        frames_by_index[index] = data

    diagnostics["error_samples"] = error_samples
    attempted = max(1, int(diagnostics["codes_attempted"]))
    failed = int(diagnostics["codes_provider_failed"])
    with_rows = int(diagnostics["codes_with_rows"])
    failure_ratio = failed / attempted
    diagnostics["provider_failure_ratio"] = round(failure_ratio, 4)
    if with_rows > 0 and failure_ratio <= 0.10:
        diagnostics["status"] = "success"
    elif with_rows > 0:
        diagnostics["status"] = "partial"
    elif failure_ratio >= 0.80:
        diagnostics["status"] = "provider_failed"
    else:
        diagnostics["status"] = "business_empty"

    print(
        "[News] ordinary stock news summary: "
        f"status={diagnostics['status']} attempted={diagnostics['codes_attempted']} "
        f"codes_with_rows={diagnostics['codes_with_rows']} rows_in_range={diagnostics['rows_in_range']} "
        f"provider_failed={diagnostics['codes_provider_failed']}",
        flush=True,
    )
    return [frames_by_index[index] for index in range(len(codes)) if index in frames_by_index], diagnostics


def normalize_event_records(
    df: pd.DataFrame | None,
    stock_pool: dict | None = None,
    source: str = "",
) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    data = df.copy()

    title_col = _first_existing_column(
        data,
        ["title", "ann_title", "news_title", "公告标题", "新闻标题", "标题", "content", "summary"],
    )
    if title_col is None:
        data["title"] = ""
    else:
        data["title"] = data[title_col].astype(str)

    summary_col = _first_existing_column(
        data,
        ["summary", "abstract", "brief", "digest", "description", "新闻摘要", "摘要"],
    )
    content_col = _first_existing_column(
        data,
        ["content", "body", "text", "article", "正文", "新闻内容", "公告内容"],
    )
    data["summary"] = data[summary_col].astype(str) if summary_col else ""
    data["content"] = data[content_col].astype(str) if content_col else ""

    if "code" not in data.columns:
        if "ts_code" in data.columns:
            data["code"] = data["ts_code"].map(ts_code_to_code)
        elif "symbol" in data.columns:
            data["code"] = data["symbol"].astype(str).str.extract(r"(\d{6})")[0]
        else:
            code_col = _first_existing_column(data, ["代码", "股票代码", "证券代码"])
            if code_col is not None:
                data["code"] = data[code_col].astype(str).str.extract(r"(\d{6})")[0]
            else:
                data["code"] = None

    data["code"] = data["code"].astype(str).str.extract(r"(\d{6})")[0]

    if stock_pool and data["code"].isna().any():
        matched_rows = []
        missing_code = data[data["code"].isna()].copy()

        for _, row in missing_code.iterrows():
            title = str(row.get("title", ""))
            for code, name in stock_pool.items():
                name = str(name or "")
                if name and name in title:
                    new_row = row.copy()
                    new_row["code"] = str(code).zfill(6)
                    new_row["name"] = name
                    matched_rows.append(new_row)

        data = data[~data["code"].isna()].copy()

        if matched_rows:
            data = pd.concat([data, pd.DataFrame(matched_rows)], ignore_index=True)

    data = data.dropna(subset=["code"]).copy()
    data["code"] = data["code"].astype(str).str.zfill(6)

    if stock_pool:
        code_set = set(stock_pool)
        data = data[data["code"].isin(code_set)].copy()
        if data.empty:
            return pd.DataFrame(columns=EVENT_COLUMNS)

    date_col = _first_existing_column(
        data,
        ["date", "ann_date", "trade_date", "publish_date", "pub_date", "公告日期", "发布日期", "新闻日期"],
    )
    time_col = _first_existing_column(
        data,
        [
            "publish_time",
            "datetime",
            "pub_time",
            "ann_time",
            "public_time",
            "time",
            "发布时间",
            "公告时间",
            "新闻时间",
        ],
    )

    if date_col is not None:
        date_raw = data[date_col].astype(str).str.slice(0, 10)
    elif time_col is not None:
        date_raw = data[time_col].astype(str).str.slice(0, 10)
    else:
        date_raw = datetime.today().strftime("%Y%m%d")

    data["date"] = pd.to_datetime(date_raw, errors="coerce")
    data = data.dropna(subset=["date"]).copy()

    if time_col is not None:
        data["publish_time"] = pd.to_datetime(data[time_col], errors="coerce")
    else:
        data["publish_time"] = data["date"]

    name_col = _first_existing_column(data, ["name", "名称", "股票简称", "证券简称"])
    if name_col is None:
        data["name"] = data["code"].map(stock_pool or {}).fillna("")
    else:
        mapped_name = data["code"].map(stock_pool or {})
        data["name"] = mapped_name.fillna(data[name_col]).fillna("")

    source_col = _first_existing_column(data, ["source", "src", "文章来源", "来源"])
    if source_col is None:
        data["source"] = source
    else:
        data["source"] = data[source_col].fillna(source)

    if "url" not in data.columns and "pdf_url" in data.columns:
        data["url"] = data["pdf_url"]
    elif "url" not in data.columns:
        url_col = _first_existing_column(data, ["新闻链接", "公告链接", "网址", "链接"])
        if url_col is not None:
            data["url"] = data[url_col]
        else:
            data["url"] = ""

    out = data[EVENT_COLUMNS].copy()
    out["title"] = out["title"].fillna("").astype(str)
    out["summary"] = out["summary"].fillna("").astype(str)
    out["content"] = out["content"].fillna("").astype(str)
    out["source"] = out["source"].fillna(source).astype(str)
    out["url"] = out["url"].fillna("").astype(str)
    out = out.drop_duplicates(subset=["date", "code", "title"], keep="last")
    out = out.sort_values(["date", "code", "publish_time"]).reset_index(drop=True)

    return out


def load_event_cache(stock_pool: dict | None = None) -> pd.DataFrame:
    frames = []

    for path, source in [
        (NEWS_CACHE_PATH, "news_cache"),
        (ANNOUNCEMENT_CACHE_PATH, "announcement_cache"),
    ]:
        path = owned_path(path)
        if not os.path.exists(path):
            continue

        try:
            raw = pd.read_csv(path, dtype={"code": str})
            frames.append(normalize_event_records(raw, stock_pool=stock_pool, source=source))
        except Exception as e:
            print(f"[News] cache read failed, path={path}: {e}")

    if not frames:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    data = pd.concat(frames, ignore_index=True)
    data = data.drop_duplicates(subset=["date", "code", "title"], keep="last")
    data = data.sort_values(["date", "code", "publish_time"]).reset_index(drop=True)
    return data


def _merge_and_save_cache(path: str, new_df: pd.DataFrame, source: str) -> pd.DataFrame:
    path = str(owned_path(path))
    old_df = pd.DataFrame(columns=EVENT_COLUMNS)

    if os.path.exists(path):
        try:
            old_df = pd.read_csv(path, dtype={"code": str})
        except Exception:
            old_df = pd.DataFrame(columns=EVENT_COLUMNS)

    data = pd.concat(
        [
            normalize_event_records(old_df, source=source),
            normalize_event_records(new_df, source=source),
        ],
        ignore_index=True,
    )
    data = data.drop_duplicates(subset=["date", "code", "title"], keep="last")
    data = data.sort_values(["date", "code", "publish_time"]).reset_index(drop=True)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    data.to_csv(path, index=False, encoding="utf-8-sig")
    return data


def fetch_tushare_announcements(
    token: str,
    stock_pool: dict | None,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    try:
        pro = init_tushare_pro(token)
        raw = pro.anns_d(
            start_date=start_date,
            end_date=end_date,
            fields="ts_code,ann_date,title,ann_type,pdf_url",
        )
        return normalize_event_records(raw, stock_pool=stock_pool, source="tushare_anns_d")
    except Exception as e:
        print(f"[News] Tushare announcement fetch skipped: {e}")
        return pd.DataFrame(columns=EVENT_COLUMNS)


def fetch_tushare_news(
    token: str,
    stock_pool: dict | None,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    try:
        pro = init_tushare_pro(token)
        start_dt = pd.to_datetime(start_date).strftime("%Y-%m-%d 00:00:00")
        end_dt = pd.to_datetime(end_date).strftime("%Y-%m-%d 23:59:59")
        raw = pro.news(
            src="sina",
            start_date=start_dt,
            end_date=end_dt,
        )
        return normalize_event_records(raw, stock_pool=stock_pool, source="tushare_news")
    except Exception as e:
        print(f"[News] Tushare news fetch skipped: {e}")
        return pd.DataFrame(columns=EVENT_COLUMNS)


def fetch_akshare_announcements(
    stock_pool: dict | None,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    if not AKSHARE_FETCH_ANNOUNCEMENTS:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    ak = _import_akshare()
    if ak is None:
        return pd.DataFrame(columns=EVENT_COLUMNS)

    try:
        try:
            raw = ak.stock_notice_report(
                report_type="全部",
                recent_page=str(max(1, AKSHARE_NOTICE_RECENT_PAGES)),
            )
        except TypeError:
            start_dt = pd.to_datetime(start_date, errors="coerce")
            end_dt = pd.to_datetime(end_date, errors="coerce")
            if pd.isna(start_dt) or pd.isna(end_dt):
                return pd.DataFrame(columns=EVENT_COLUMNS)
            start_dt = max(start_dt, end_dt - pd.Timedelta(days=max(1, AKSHARE_NOTICE_MAX_DAYS) - 1))
            raw = _fetch_akshare_announcement_dates(ak, start_dt, end_dt)
            data = normalize_event_records(
                raw,
                stock_pool=stock_pool,
                source="akshare_stock_notice_report",
            )
            return _date_in_range(data, start_date=start_date, end_date=end_date)
        data = normalize_event_records(
            raw,
            stock_pool=stock_pool,
            source="akshare_stock_notice_report",
        )
        return _date_in_range(data, start_date=start_date, end_date=end_date)
    except Exception as e:
        print(f"[News] AkShare announcement fetch skipped: {e}")
        return pd.DataFrame(columns=EVENT_COLUMNS)



def _eastmoney_http_get(url: str, *, params: dict, headers: dict, timeout: float):
    """Use AKShare's current curl_cffi transport when available, then requests fallback."""
    try:
        from curl_cffi import requests as curl_requests  # type: ignore

        return curl_requests.get(url, params=params, headers=headers, timeout=timeout)
    except Exception as curl_error:
        try:
            return requests.get(url, params=params, headers=headers, timeout=timeout)
        except Exception as requests_error:
            raise RuntimeError(
                f"eastmoney_http_failed:curl={type(curl_error).__name__}:{curl_error}; "
                f"requests={type(requests_error).__name__}:{requests_error}"
            ) from requests_error


def _eastmoney_jsonp_rows(
    keyword: str,
    *,
    page_index: int,
    page_size: int,
) -> list[dict]:
    callback = f"jQuery3510{int(time.time() * 1000)}{page_index}"
    inner_param = {
        "uid": "",
        "keyword": str(keyword),
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "default",
                "pageIndex": int(page_index),
                "pageSize": int(page_size),
                "preTag": "<em>",
                "postTag": "</em>",
            }
        },
    }
    url = "https://search-api-web.eastmoney.com/search/jsonp"
    params = {
        "cb": callback,
        "param": json.dumps(inner_param, ensure_ascii=False),
        "_": str(int(time.time() * 1000)),
    }
    headers = {
        "Accept": "*/*",
        "Referer": f"https://so.eastmoney.com/news/s?keyword={quote(str(keyword), safe='')}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36"
        ),
    }
    response = _eastmoney_http_get(
        url,
        params=params,
        headers=headers,
        timeout=max(3.0, EASTMONEY_STOCK_NEWS_TIMEOUT_SECONDS),
    )
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    text = str(getattr(response, "text", "") or "").strip()
    left = text.find("(")
    right = text.rfind(")")
    if left < 0 or right <= left:
        raise ValueError("eastmoney_stock_news_invalid_jsonp")
    data_json = json.loads(text[left + 1:right])
    result = data_json.get("result") or {}
    rows = result.get("cmsArticleWebOld") or []
    return list(rows) if isinstance(rows, list) else []


def _direct_eastmoney_stock_news(
    keyword: str,
    *,
    canonical_code: str,
    start_date: str = "",
    end_date: str = "",
) -> pd.DataFrame:
    """Direct Eastmoney compatibility path with pagination and date filtering.

    Search snippets are not treated as full article bodies. The caller keeps them
    as ``summary`` and the full-text ingestion layer fetches each article URL.
    """
    frames: list[pd.DataFrame] = []
    max_pages = max(1, min(10, int(EASTMONEY_STOCK_NEWS_MAX_PAGES)))
    page_size = max(10, min(100, int(EASTMONEY_STOCK_NEWS_PAGE_SIZE)))
    start_dt = pd.to_datetime(start_date, errors="coerce") if start_date else pd.NaT
    end_dt = pd.to_datetime(end_date, errors="coerce") if end_date else pd.NaT

    found_in_range = False
    for page_index in range(1, max_pages + 1):
        rows = _eastmoney_jsonp_rows(keyword, page_index=page_index, page_size=page_size)
        if not rows:
            break
        frame = pd.DataFrame(rows)
        if "code" in frame.columns:
            frame["新闻链接"] = "http://finance.eastmoney.com/a/" + frame["code"].astype(str) + ".html"
        else:
            frame["新闻链接"] = ""
        frame.rename(
            columns={
                "date": "发布时间",
                "mediaName": "文章来源",
                "title": "新闻标题",
                "content": "新闻内容",
            },
            inplace=True,
        )
        frame["关键词"] = str(canonical_code).zfill(6)
        for col in ["新闻标题", "新闻内容"]:
            if col not in frame.columns:
                frame[col] = ""
            frame[col] = (
                frame[col].astype(str)
                .str.replace("<em>", "", regex=False)
                .str.replace("</em>", "", regex=False)
                .str.replace("\u3000", "", regex=False)
                .str.replace("\r\n", " ", regex=False)
            )
        for col in ["发布时间", "文章来源", "新闻链接"]:
            if col not in frame.columns:
                frame[col] = ""
        frames.append(frame[["关键词", "新闻标题", "新闻内容", "发布时间", "文章来源", "新闻链接"]])

        # Eastmoney/AKShare may return fewer rows than the requested page size.
        # Do not treat a short page as end-of-pagination; that was the failure mode
        # that could miss recent rows on Windows. Stop only after the requested
        # window has been seen and the next page no longer overlaps it.
        dates = pd.to_datetime(frame["发布时间"], errors="coerce")
        if not pd.isna(start_dt) or not pd.isna(end_dt):
            page_mask = pd.Series(True, index=frame.index)
            if not pd.isna(start_dt):
                page_mask &= dates >= start_dt.normalize()
            if not pd.isna(end_dt):
                page_mask &= dates <= (end_dt.normalize() + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1))
            page_has_in_range = bool(page_mask.fillna(False).any())
            if page_has_in_range:
                found_in_range = True
            elif found_in_range:
                break

    if not frames:
        return pd.DataFrame()
    data = pd.concat(frames, ignore_index=True)
    data = data.drop_duplicates(subset=["新闻标题", "发布时间", "文章来源", "新闻链接"], keep="first")
    if not pd.isna(start_dt) or not pd.isna(end_dt):
        dates = pd.to_datetime(data["发布时间"], errors="coerce")
        mask = pd.Series(True, index=data.index)
        if not pd.isna(start_dt):
            mask &= dates >= start_dt.normalize()
        if not pd.isna(end_dt):
            mask &= dates <= (end_dt.normalize() + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1))
        data = data[mask].copy()
    return data.reset_index(drop=True)


def _fetch_stock_entity_metadata_eastmoney(code: str) -> dict[str, str]:
    normalized = str(code).zfill(6)
    market_code = 1 if normalized.startswith("6") else 0
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"fields": "f57,f58,f127", "secid": f"{market_code}.{normalized}"}
    response = requests.get(url, params=params, timeout=8)
    response.raise_for_status()
    data = (response.json() or {}).get("data") or {}
    return {
        "stock_code": normalized,
        "stock_name": str(data.get("f58") or ""),
        "full_name": "",
        "industry": str(data.get("f127") or ""),
    }


def fetch_stock_entity_metadata(
    token: str | None,
    codes: list[str],
) -> dict[str, dict[str, str]]:
    """Resolve stock name/full-name/industry with Tushare then Eastmoney fallback."""
    normalized_codes = list(dict.fromkeys(str(code).zfill(6) for code in codes if str(code).strip()))
    result: dict[str, dict[str, str]] = {
        code: dict(_STOCK_ENTITY_METADATA_RUNTIME_CACHE[code])
        for code in normalized_codes
        if code in _STOCK_ENTITY_METADATA_RUNTIME_CACHE
    }
    unresolved = [
        code for code in normalized_codes
        if code not in result or not str(result[code].get("industry") or "").strip()
    ]
    if token and unresolved:
        try:
            pro = init_tushare_pro(token)
            basic = pro.stock_basic(
                exchange="",
                list_status="L",
                fields="symbol,name,industry,fullname",
            )
            if basic is not None and not basic.empty:
                basic = basic.copy()
                basic["symbol"] = basic["symbol"].astype(str).str.zfill(6)
                wanted = basic[basic["symbol"].isin(set(unresolved))]
                for row in wanted.to_dict(orient="records"):
                    code = str(row.get("symbol") or "").zfill(6)
                    result[code] = {
                        "stock_code": code,
                        "stock_name": str(row.get("name") or ""),
                        "full_name": str(row.get("fullname") or ""),
                        "industry": str(row.get("industry") or ""),
                    }
        except Exception as exc:
            print(f"[News] Tushare stock metadata skipped: {exc}")

    missing = [
        code for code in normalized_codes
        if code not in result or not str(result[code].get("industry") or "").strip()
    ]
    if missing:
        workers = max(1, min(_resolve_akshare_fetch_workers(len(missing)), 8))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {executor.submit(_fetch_stock_entity_metadata_eastmoney, code): code for code in missing}
            for future in as_completed(future_map):
                code = future_map[future]
                try:
                    row = future.result()
                except Exception as exc:
                    print(f"[News] Eastmoney stock metadata skipped for {code}: {exc}")
                    continue
                current = result.get(code) or {"stock_code": code, "stock_name": "", "full_name": "", "industry": ""}
                for key in ("stock_name", "full_name", "industry"):
                    if not str(current.get(key) or "").strip() and str(row.get(key) or "").strip():
                        current[key] = str(row.get(key) or "")
                result[code] = current
    for code, row in result.items():
        _STOCK_ENTITY_METADATA_RUNTIME_CACHE[code] = dict(row)
    return result


def _call_akshare_stock_news(
    ak,
    code: str,
    *,
    stock_name: str = "",
    start_date: str = "",
    end_date: str = "",
) -> tuple[pd.DataFrame, dict[str, object]]:
    last_error: Exception | None = None
    # Keep AKShare first for API compatibility. If upstream parsing breaks or
    # the returned rows do not cover the requested date, use the same Eastmoney
    # search endpoint directly.
    for kwargs in [{"stock": code}, {"symbol": code}]:
        try:
            raw = ak.stock_news_em(**kwargs)
            if raw is not None and not raw.empty:
                candidate = raw.copy()
                time_col = _first_existing_column(candidate, ["发布时间", "publish_time", "date", "datetime"])
                if time_col is not None and start_date and end_date:
                    dates = pd.to_datetime(candidate[time_col], errors="coerce")
                    start_dt = pd.to_datetime(start_date, errors="coerce")
                    end_dt = pd.to_datetime(end_date, errors="coerce")
                    in_range = candidate[(dates >= start_dt.normalize()) & (dates <= end_dt.normalize() + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1))].copy()
                else:
                    in_range = candidate
                if not in_range.empty:
                    return in_range, {
                        "status": "success",
                        "provider": "akshare",
                        "raw_rows": int(len(candidate)),
                        "in_range_rows": int(len(in_range)),
                    }
        except TypeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            break

    direct_errors: list[str] = []
    try:
        direct = _direct_eastmoney_stock_news(
            code,
            canonical_code=code,
            start_date=start_date,
            end_date=end_date,
        )
        if not direct.empty:
            return direct, {
                "status": "success",
                "provider": "eastmoney_direct",
                "raw_rows": int(len(direct)),
                "in_range_rows": int(len(direct)),
                "akshare_error": str(last_error or ""),
            }
    except Exception as exc:
        direct_errors.append(f"code:{type(exc).__name__}:{exc}")

    if stock_name:
        try:
            by_name = _direct_eastmoney_stock_news(
                stock_name,
                canonical_code=code,
                start_date=start_date,
                end_date=end_date,
            )
            if not by_name.empty:
                return by_name, {
                    "status": "success",
                    "provider": "eastmoney_name",
                    "raw_rows": int(len(by_name)),
                    "in_range_rows": int(len(by_name)),
                    "akshare_error": str(last_error or ""),
                }
        except Exception as exc:
            direct_errors.append(f"name:{type(exc).__name__}:{exc}")

    if direct_errors and last_error is not None:
        return pd.DataFrame(), {
            "status": "provider_failed",
            "provider": "akshare_eastmoney",
            "raw_rows": 0,
            "in_range_rows": 0,
            "error": f"akshare={type(last_error).__name__}:{last_error}; {'; '.join(direct_errors)}",
        }
    return pd.DataFrame(), {
        "status": "business_empty",
        "provider": "akshare_eastmoney",
        "raw_rows": 0,
        "in_range_rows": 0,
        "error": "; ".join(direct_errors),
    }


def fetch_akshare_stock_news(
    stock_pool: dict | None,
    start_date: str,
    end_date: str,
    *,
    return_status: bool = False,
):
    empty = pd.DataFrame(columns=EVENT_COLUMNS)
    base_status: dict[str, object] = {
        "status": "skipped",
        "codes_attempted": 0,
        "codes_with_rows": 0,
        "codes_business_empty": 0,
        "codes_provider_failed": 0,
        "rows_raw": 0,
        "rows_in_range": 0,
        "error_samples": [],
    }
    if not AKSHARE_FETCH_STOCK_NEWS:
        return (empty, base_status) if return_status else empty
    if not stock_pool:
        base_status["status"] = "business_empty"
        return (empty, base_status) if return_status else empty

    ak = _import_akshare()
    if ak is None:
        base_status["status"] = "provider_failed"
        base_status["error_samples"] = ["akshare_import_failed"]
        return (empty, base_status) if return_status else empty

    codes = list(stock_pool.keys())[: max(1, AKSHARE_STOCK_NEWS_MAX_CODES)]
    frames, status = _fetch_akshare_stock_news_frames(
        ak,
        stock_pool,
        codes,
        start_date=start_date,
        end_date=end_date,
    )
    if not frames:
        return (empty, status) if return_status else empty

    data = pd.concat(frames, ignore_index=True)
    data = normalize_event_records(
        data,
        stock_pool=stock_pool,
        source="akshare_stock_news_em",
    )
    # Eastmoney search result "新闻内容" is a search snippet, not the canonical article body.
    # Keep it as summary and force the full-text ingestion layer to fetch the article URL.
    if not data.empty:
        snippet = data["content"].fillna("").astype(str)
        summary = data["summary"].fillna("").astype(str)
        data["summary"] = summary.where(summary.str.strip().ne(""), snippet)
        data["content"] = ""
    data = _date_in_range(data, start_date=start_date, end_date=end_date)
    status["rows_in_range"] = int(len(data))
    if len(data) > 0 and status.get("status") == "business_empty":
        status["status"] = "success"
    return (data, status) if return_status else data


def refresh_news_event_cache(
    token: str | None,
    stock_pool: dict | None,
    start_date,
    end_date,
) -> tuple[pd.DataFrame, dict]:
    status = {
        "news_enabled": bool(token) or bool(ENABLE_AKSHARE_NEWS_FALLBACK),
        "announcement_rows_fetched": 0,
        "news_rows_fetched": 0,
        "akshare_enabled": bool(ENABLE_AKSHARE_NEWS_FALLBACK),
        "akshare_announcement_rows_fetched": 0,
        "akshare_news_rows_fetched": 0,
        "cache_rows": 0,
        "data_source_action": "cache_or_zero",
    }

    ann_df = pd.DataFrame(columns=EVENT_COLUMNS)
    news_df = pd.DataFrame(columns=EVENT_COLUMNS)
    start = pd.to_datetime(start_date).strftime("%Y%m%d")
    end = pd.to_datetime(end_date).strftime("%Y%m%d")

    if token:
        ann_df = fetch_tushare_announcements(
            token=token,
            stock_pool=stock_pool,
            start_date=start,
            end_date=end,
        )
        news_df = fetch_tushare_news(
            token=token,
            stock_pool=stock_pool,
            start_date=start,
            end_date=end,
        )

        status["announcement_rows_fetched"] = int(len(ann_df))
        status["news_rows_fetched"] = int(len(news_df))

    if ENABLE_AKSHARE_NEWS_FALLBACK:
        if ann_df.empty:
            ak_ann_df = fetch_akshare_announcements(
                stock_pool=stock_pool,
                start_date=start,
                end_date=end,
            )
            status["akshare_announcement_rows_fetched"] = int(len(ak_ann_df))
            if not ak_ann_df.empty:
                ann_df = ak_ann_df

        if news_df.empty:
            ak_news_df, ordinary_status = fetch_akshare_stock_news(
                stock_pool=stock_pool,
                start_date=start,
                end_date=end,
                return_status=True,
            )
            status["akshare_news_rows_fetched"] = int(len(ak_news_df))
            status["ordinary_news_status"] = ordinary_status
            if not ak_news_df.empty:
                news_df = ak_news_df

    if not ann_df.empty:
        _merge_and_save_cache(ANNOUNCEMENT_CACHE_PATH, ann_df, "announcement_cache")
        status["data_source_action"] = "fetched_or_cache"

    if not news_df.empty:
        _merge_and_save_cache(NEWS_CACHE_PATH, news_df, "news_cache")
        status["data_source_action"] = "fetched_or_cache"

    cache = load_event_cache(stock_pool=stock_pool)
    status["cache_rows"] = int(len(cache))
    return cache, status
