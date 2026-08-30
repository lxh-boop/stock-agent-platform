from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

import pandas as pd

from local_config import load_local_config
from universe import get_stock_pool
from .schema import NEWS_MAPPING_DB_PATH, execute_many, get_connection, init_db


SOURCE_TUSHARE_STOCK_BASIC = "tushare_stock_basic"
SOURCE_LOCAL_CSI300_CACHE = "local_csi300_cache"


COMPANY_SUFFIX_PATTERNS = [
    "股份有限公司",
    "集团股份有限公司",
    "集团有限公司",
    "控股股份有限公司",
    "控股有限公司",
    "有限责任公司",
    "有限公司",
    "股份",
    "集团",
]


def normalize_code(value) -> str:
    text = str(value or "").strip()
    if "." in text:
        text = text.split(".")[0]
    return text.zfill(6)


def normalize_alias(value: str | None) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "", text)
    return text


def strip_company_suffix(value: str | None) -> str:
    text = normalize_alias(value)
    for suffix in COMPANY_SUFFIX_PATTERNS:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text


def st_name_variants(name: str | None) -> list[str]:
    text = normalize_alias(name)
    variants = []
    for prefix in ["*ST", "ST", "S*ST", "SST"]:
        if text.upper().startswith(prefix.upper()):
            variants.append(text[len(prefix):])
    return [v for v in variants if v]


def add_alias(
    aliases: dict[str, dict],
    alias: str | None,
    code: str,
    name: str,
    source: str,
    confidence: float,
) -> None:
    alias = normalize_alias(alias)
    if not alias:
        return
    if len(alias) < 2 and not alias.isdigit():
        return
    key = alias
    current = aliases.get(key)
    if current is None or confidence > current["confidence"]:
        aliases[key] = {
            "alias": alias,
            "code": code,
            "name": name,
            "source": source,
            "confidence": float(confidence),
        }


def build_aliases_for_stock(row: dict, source: str) -> list[dict]:
    code = normalize_code(row.get("code") or row.get("symbol") or row.get("ts_code"))
    name = normalize_alias(row.get("name"))
    fullname = normalize_alias(row.get("fullname"))
    enname = normalize_alias(row.get("enname"))

    aliases: dict[str, dict] = {}
    add_alias(aliases, code, code, name, source, 1.0)
    add_alias(aliases, row.get("ts_code"), code, name, source, 1.0)
    add_alias(aliases, name, code, name, source, 1.0)
    add_alias(aliases, fullname, code, name, source, 0.98)
    add_alias(aliases, strip_company_suffix(fullname), code, name, source, 0.88)
    add_alias(aliases, enname, code, name, source, 0.75)

    for variant in st_name_variants(name):
        add_alias(aliases, variant, code, name, source, 0.72)

    return list(aliases.values())


def fetch_stock_basic_from_tushare(token: str) -> pd.DataFrame:
    try:
        import tushare as ts
    except Exception as exc:
        raise RuntimeError("缺少 tushare，请先运行：pip install tushare") from exc

    pro = ts.pro_api(token)
    fields = "ts_code,symbol,name,area,industry,fullname,enname,list_date"
    df = pro.stock_basic(exchange="", list_status="L", fields=fields)
    if df is None or df.empty:
        raise RuntimeError("Tushare stock_basic 返回为空，请检查 Token 权限或网络。")
    df = df.copy()
    df["code"] = df["symbol"].astype(str).str.zfill(6)
    return df


def load_stock_basic(token: str | None = None) -> tuple[pd.DataFrame, str]:
    if token:
        return fetch_stock_basic_from_tushare(token), SOURCE_TUSHARE_STOCK_BASIC

    pool = get_stock_pool(token=None, enrich_name=False)
    rows = [
        {
            "code": normalize_code(code),
            "ts_code": "",
            "symbol": normalize_code(code),
            "name": name,
            "area": "",
            "industry": "",
            "fullname": name,
            "enname": "",
            "list_date": "",
        }
        for code, name in pool.items()
    ]
    return pd.DataFrame(rows), SOURCE_LOCAL_CSI300_CACHE


def upsert_stock_master_and_aliases(
    stock_basic: pd.DataFrame,
    source: str,
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
) -> dict:
    init_db(db_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    stock_rows = []
    alias_rows = []

    for _, row in stock_basic.fillna("").iterrows():
        item = row.to_dict()
        code = normalize_code(item.get("code") or item.get("symbol") or item.get("ts_code"))
        name = normalize_alias(item.get("name"))
        aliases = build_aliases_for_stock(item, source=source)
        stock_rows.append(
            {
                "code": code,
                "ts_code": item.get("ts_code", ""),
                "name": name,
                "fullname": normalize_alias(item.get("fullname")),
                "industry": normalize_alias(item.get("industry")),
                "area": normalize_alias(item.get("area")),
                "list_date": str(item.get("list_date", "")),
                "aliases": json.dumps(
                    [a["alias"] for a in aliases],
                    ensure_ascii=False,
                ),
                "updated_at": now,
            }
        )
        for alias in aliases:
            alias_rows.append({**alias, "updated_at": now})

    with get_connection(db_path) as conn:
        execute_many(conn, 
            """
            INSERT INTO news_mapping_stock_master
                (code, ts_code, name, fullname, industry, area, list_date, aliases, updated_at)
            VALUES
                (%(code)s, %(ts_code)s, %(name)s, %(fullname)s, %(industry)s, %(area)s, %(list_date)s, %(aliases)s, %(updated_at)s)
            ON CONFLICT(code) DO UPDATE SET
                ts_code=excluded.ts_code,
                name=excluded.name,
                fullname=excluded.fullname,
                industry=excluded.industry,
                area=excluded.area,
                list_date=excluded.list_date,
                aliases=excluded.aliases,
                updated_at=excluded.updated_at
            """,
            stock_rows,
        )
        execute_many(conn, 
            """
            INSERT INTO news_mapping_stock_alias
                (alias, code, name, source, confidence, updated_at)
            VALUES
                (%(alias)s, %(code)s, %(name)s, %(source)s, %(confidence)s, %(updated_at)s)
            ON CONFLICT(alias, code) DO UPDATE SET
                name=excluded.name,
                source=excluded.source,
                confidence=GREATEST(news_mapping_stock_alias.confidence, excluded.confidence),
                updated_at=excluded.updated_at
            """,
            alias_rows,
        )
        conn.commit()

    return {
        "db_path": str(db_path),
        "source": source,
        "stock_rows": len(stock_rows),
        "alias_rows": len(alias_rows),
        "updated_at": now,
    }


def build_stock_alias_table(
    token: str | None = None,
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
) -> dict:
    stock_basic, source = load_stock_basic(token=token)
    return upsert_stock_master_and_aliases(
        stock_basic=stock_basic,
        source=source,
        db_path=db_path,
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", type=str, default="")
    parser.add_argument("--db-path", type=str, default=str(NEWS_MAPPING_DB_PATH))
    parser.add_argument(
        "--use-local-config-token",
        action="store_true",
        help="从 local_app_config.json 读取 Tushare Token；不会打印 Token。",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = args.token.strip() or os.getenv("TUSHARE_TOKEN", "").strip()
    if not token and args.use_local_config_token:
        token = str(load_local_config().get("tushare_token") or "").strip()

    report = build_stock_alias_table(
        token=token or None,
        db_path=args.db_path,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
