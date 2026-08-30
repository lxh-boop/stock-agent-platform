from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from .schema import NEWS_MAPPING_DB_PATH, get_connection, init_db


DEFAULT_CONCEPT_WORDS = [
    "稀土",
    "光伏",
    "半导体",
    "芯片",
    "算力",
    "人工智能",
    "AI",
    "银行",
    "保险",
    "券商",
    "证券",
    "煤炭",
    "钢铁",
    "有色金属",
    "新能源车",
    "新能源汽车",
    "锂电池",
    "白酒",
    "医药",
    "创新药",
    "房地产",
    "电力",
    "军工",
]

RISK_KEYWORDS = [
    "处罚",
    "诉讼",
    "仲裁",
    "立案",
    "减持",
    "亏损",
    "退市",
    "风险",
    "违约",
    "债务",
]

POSITIVE_KEYWORDS = [
    "回购",
    "增持",
    "中标",
    "合同",
    "盈利",
    "增长",
    "分红",
    "重组",
]


def normalize_code(value: str) -> str:
    return str(value or "").strip().zfill(6)


def normalize_text(*parts: Any) -> str:
    return "\n".join(str(p or "") for p in parts)


def load_alias_table(db_path: str | Path = NEWS_MAPPING_DB_PATH) -> pd.DataFrame:
    init_db(db_path)
    with get_connection(db_path) as conn:
        return pd.read_sql_query(
            """
            SELECT alias, code, name, source, confidence
            FROM news_mapping_stock_alias
            WHERE alias IS NOT NULL AND alias != ''
            """,
            conn,
        )


def load_stock_master(db_path: str | Path = NEWS_MAPPING_DB_PATH) -> pd.DataFrame:
    init_db(db_path)
    with get_connection(db_path) as conn:
        return pd.read_sql_query(
            """
            SELECT code, name, fullname, industry, area
            FROM news_mapping_stock_master
            """,
            conn,
        )


def find_code_mentions(text: str, stock_master: pd.DataFrame) -> list[dict]:
    codes = set(stock_master["code"].astype(str).str.zfill(6))
    name_map = dict(
        zip(stock_master["code"].astype(str).str.zfill(6), stock_master["name"].fillna(""))
    )
    matches = []

    for raw in re.findall(r"(?<!\d)(\d{6})(?:\.(?:SH|SZ|BJ))?(?!\d)", text, flags=re.I):
        code = normalize_code(raw)
        if code in codes:
            matches.append(
                {
                    "code": code,
                    "name": name_map.get(code, ""),
                    "matched_text": raw,
                    "match_type": "stock_code",
                    "confidence": 1.0,
                }
            )
    return matches


def find_alias_mentions(text: str, alias_df: pd.DataFrame) -> list[dict]:
    if alias_df.empty:
        return []

    candidates = alias_df.copy()
    candidates["alias"] = candidates["alias"].astype(str).str.strip()
    candidates["alias_len"] = candidates["alias"].str.len()
    candidates = candidates[candidates["alias_len"] >= 2].copy()
    candidates = candidates.sort_values(["alias_len", "confidence"], ascending=[False, False])

    matches_by_key: dict[tuple[str, str], dict] = {}

    for row in candidates.itertuples(index=False):
        alias = str(row.alias)
        if not alias:
            continue
        if alias.isdigit():
            continue
        if alias in text:
            key = (str(row.code).zfill(6), alias)
            matches_by_key[key] = {
                "code": str(row.code).zfill(6),
                "name": str(row.name or ""),
                "matched_text": alias,
                "match_type": "stock_alias",
                "confidence": float(row.confidence or 0.0),
                "source": str(row.source or ""),
            }

    best_by_code: dict[str, dict] = {}
    for item in matches_by_key.values():
        code = item["code"]
        current = best_by_code.get(code)
        if current is None:
            best_by_code[code] = item
            continue
        if (item["confidence"], len(item["matched_text"])) > (
            current["confidence"],
            len(current["matched_text"]),
        ):
            best_by_code[code] = item

    return list(best_by_code.values())


def find_concepts(text: str, stock_master: pd.DataFrame) -> list[dict]:
    found: dict[str, dict] = {}

    for concept in DEFAULT_CONCEPT_WORDS:
        if concept and concept in text:
            found[concept] = {
                "concept": concept,
                "match_type": "default_concept",
                "confidence": 0.65,
            }

    for industry in sorted(set(stock_master["industry"].dropna().astype(str))):
        industry = industry.strip()
        if len(industry) >= 2 and industry in text:
            found[industry] = {
                "concept": industry,
                "match_type": "industry",
                "confidence": 0.70,
            }

    return list(found.values())


def find_regions(text: str, stock_master: pd.DataFrame) -> list[dict]:
    regions = []
    for area in sorted(set(stock_master["area"].dropna().astype(str))):
        area = area.strip()
        if len(area) >= 2 and area in text:
            regions.append(
                {
                    "region": area,
                    "match_type": "area",
                    "confidence": 0.50,
                }
            )
    return regions


def find_keywords(text: str) -> list[dict]:
    rows = []
    for word in RISK_KEYWORDS:
        if word in text:
            rows.append({"keyword": word, "keyword_type": "risk"})
    for word in POSITIVE_KEYWORDS:
        if word in text:
            rows.append({"keyword": word, "keyword_type": "positive"})
    return rows


def extract_entities_from_text(
    title: str,
    content: str = "",
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
) -> dict:
    text = normalize_text(title, content)
    alias_df = load_alias_table(db_path)
    stock_master = load_stock_master(db_path)

    code_matches = find_code_mentions(text, stock_master)
    alias_matches = find_alias_mentions(text, alias_df)

    stocks_by_code: dict[str, dict] = {}
    for item in [*alias_matches, *code_matches]:
        code = item["code"]
        current = stocks_by_code.get(code)
        if current is None or item["confidence"] > current["confidence"]:
            stocks_by_code[code] = item

    concepts = find_concepts(text, stock_master)
    regions = find_regions(text, stock_master)
    keywords = find_keywords(text)

    return {
        "candidate_entities": {
            "stock_alias_matches": alias_matches,
            "stock_code_matches": code_matches,
            "region_matches": regions,
            "keyword_matches": keywords,
        },
        "candidate_stocks": list(stocks_by_code.values()),
        "candidate_concepts": concepts,
    }


def extract_entities_for_news_row(row: dict, db_path: str | Path = NEWS_MAPPING_DB_PATH) -> dict:
    return extract_entities_from_text(
        title=str(row.get("title") or ""),
        content=str(row.get("content") or ""),
        db_path=db_path,
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=str, default=str(NEWS_MAPPING_DB_PATH))
    parser.add_argument("--limit", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_db(args.db_path)
    with get_connection(args.db_path) as conn:
        news = pd.read_sql_query(
            """
            SELECT news_id, title, content
            FROM news_mapping_news_items
            ORDER BY publish_time DESC
            LIMIT %s
            """,
            conn,
            params=(args.limit,),
        )

    outputs = []
    for row in news.to_dict(orient="records"):
        extracted = extract_entities_for_news_row(row, db_path=args.db_path)
        outputs.append(
            {
                "news_id": row["news_id"],
                "title": row["title"],
                "candidate_stock_count": len(extracted["candidate_stocks"]),
                "candidate_concept_count": len(extracted["candidate_concepts"]),
                "candidate_stocks": extracted["candidate_stocks"][:10],
                "candidate_concepts": extracted["candidate_concepts"][:10],
            }
        )
    print(json.dumps(outputs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
