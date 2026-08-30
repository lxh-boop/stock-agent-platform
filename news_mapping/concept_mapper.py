from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .schema import NEWS_MAPPING_DB_PATH, execute_many, get_connection, init_db


DEFAULT_CONCEPT_RULES = {
    "稀土": {
        "industry_keywords": ["稀土"],
        "name_keywords": ["稀土"],
        "relation_type": "raw_material",
        "confidence": 0.70,
    },
    "光伏": {
        "industry_keywords": ["光伏"],
        "name_keywords": ["光伏", "太阳能"],
        "relation_type": "theme",
        "confidence": 0.60,
    },
    "半导体": {
        "industry_keywords": ["半导体"],
        "name_keywords": ["半导体", "芯片", "微电"],
        "relation_type": "industry_member",
        "confidence": 0.62,
    },
    "芯片": {
        "industry_keywords": ["半导体"],
        "name_keywords": ["芯片", "半导体", "微电"],
        "relation_type": "theme",
        "confidence": 0.60,
    },
    "算力": {
        "industry_keywords": [],
        "name_keywords": ["算力", "数据中心", "云计算"],
        "relation_type": "theme",
        "confidence": 0.50,
    },
    "人工智能": {
        "industry_keywords": [],
        "name_keywords": ["人工智能", "AI", "智能"],
        "relation_type": "theme",
        "confidence": 0.50,
    },
    "银行": {
        "industry_keywords": ["银行"],
        "name_keywords": ["银行"],
        "relation_type": "industry_member",
        "confidence": 0.72,
    },
    "保险": {
        "industry_keywords": ["保险"],
        "name_keywords": ["保险"],
        "relation_type": "industry_member",
        "confidence": 0.72,
    },
    "券商": {
        "industry_keywords": ["证券"],
        "name_keywords": ["证券"],
        "relation_type": "industry_member",
        "confidence": 0.72,
    },
    "证券": {
        "industry_keywords": ["证券"],
        "name_keywords": ["证券"],
        "relation_type": "industry_member",
        "confidence": 0.72,
    },
    "煤炭": {
        "industry_keywords": ["煤炭"],
        "name_keywords": ["煤"],
        "relation_type": "industry_member",
        "confidence": 0.68,
    },
    "钢铁": {
        "industry_keywords": ["钢铁"],
        "name_keywords": ["钢", "铁"],
        "relation_type": "industry_member",
        "confidence": 0.68,
    },
    "有色金属": {
        "industry_keywords": ["有色金属", "铝", "铜", "小金属"],
        "name_keywords": ["铜", "铝", "锌", "钼", "钨"],
        "relation_type": "industry_member",
        "confidence": 0.62,
    },
    "新能源车": {
        "industry_keywords": ["汽车整车", "汽车配件"],
        "name_keywords": ["比亚迪", "长安汽车", "长城汽车"],
        "relation_type": "theme",
        "confidence": 0.55,
    },
    "锂电池": {
        "industry_keywords": ["锂电池"],
        "name_keywords": ["锂", "电池"],
        "relation_type": "theme",
        "confidence": 0.58,
    },
    "白酒": {
        "industry_keywords": ["白酒"],
        "name_keywords": ["茅台", "五粮液", "泸州老窖", "贡酒", "酒鬼酒"],
        "relation_type": "industry_member",
        "confidence": 0.75,
    },
    "医药": {
        "industry_keywords": ["医药", "医疗保健", "生物制药"],
        "name_keywords": [],
        "relation_type": "industry_member",
        "confidence": 0.65,
    },
    "创新药": {
        "industry_keywords": ["医药", "生物制药"],
        "name_keywords": ["创新药"],
        "relation_type": "theme",
        "confidence": 0.55,
    },
    "房地产": {
        "industry_keywords": ["房地产"],
        "name_keywords": ["地产", "置业"],
        "relation_type": "industry_member",
        "confidence": 0.72,
    },
    "电力": {
        "industry_keywords": ["电力"],
        "name_keywords": ["电力", "水电"],
        "relation_type": "industry_member",
        "confidence": 0.68,
    },
    "军工": {
        "industry_keywords": ["航空", "船舶", "国防军工"],
        "name_keywords": ["航天", "航空", "兵器", "军工", "卫星"],
        "relation_type": "theme",
        "confidence": 0.58,
    },
}


def _contains_any(text: str, keywords: list[str]) -> str | None:
    text = str(text or "")
    for keyword in keywords:
        if keyword and keyword in text:
            return keyword
    return None


def load_stock_master(db_path: str | Path = NEWS_MAPPING_DB_PATH) -> pd.DataFrame:
    init_db(db_path)
    with get_connection(db_path) as conn:
        return pd.read_sql_query(
            "SELECT code, name, fullname, industry, area FROM news_mapping_stock_master",
            conn,
        )


def seed_default_concept_stock_map(db_path: str | Path = NEWS_MAPPING_DB_PATH) -> dict:
    init_db(db_path)
    stock_master = load_stock_master(db_path).fillna("")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []

    for concept, rule in DEFAULT_CONCEPT_RULES.items():
        for _, stock in stock_master.iterrows():
            industry_hit = _contains_any(
                stock.get("industry", ""),
                rule.get("industry_keywords", []),
            )
            name_text = f"{stock.get('name', '')} {stock.get('fullname', '')}"
            name_hit = _contains_any(name_text, rule.get("name_keywords", []))

            if not industry_hit and not name_hit:
                continue

            evidence_parts = []
            if industry_hit:
                evidence_parts.append(f"industry contains {industry_hit}")
            if name_hit:
                evidence_parts.append(f"name/fullname contains {name_hit}")

            rows.append(
                {
                    "concept": concept,
                    "code": str(stock["code"]).zfill(6),
                    "name": stock.get("name", ""),
                    "relation_type": rule.get("relation_type", "theme"),
                    "confidence": float(rule.get("confidence", 0.5)),
                    "evidence": "; ".join(evidence_parts),
                    "source": "default_rule",
                    "updated_at": now,
                }
            )

    with get_connection(db_path) as conn:
        conn.execute("DELETE FROM news_mapping_concept_stock_map WHERE source = 'default_rule'")
        execute_many(conn, 
            """
            INSERT INTO news_mapping_concept_stock_map
                (concept, code, name, relation_type, confidence, evidence, source, updated_at)
            VALUES
                (%(concept)s, %(code)s, %(name)s, %(relation_type)s, %(confidence)s, %(evidence)s, %(source)s, %(updated_at)s)
            ON CONFLICT(concept, code, relation_type) DO UPDATE SET
                name=excluded.name,
                confidence=GREATEST(news_mapping_concept_stock_map.confidence, excluded.confidence),
                evidence=excluded.evidence,
                source=excluded.source,
                updated_at=excluded.updated_at
            """,
            rows,
        )
        conn.commit()

    return {
        "db_path": str(db_path),
        "concept_count": len(DEFAULT_CONCEPT_RULES),
        "upsert_rows": len(rows),
        "updated_at": now,
    }


def map_concepts_to_stocks(
    concepts: list[str] | list[dict],
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
    max_stocks_per_concept: int = 50,
) -> list[dict]:
    init_db(db_path)
    concept_names = []
    for item in concepts:
        if isinstance(item, dict):
            value = item.get("concept")
        else:
            value = item
        value = str(value or "").strip()
        if value:
            concept_names.append(value)

    if not concept_names:
        return []

    rows = []
    with get_connection(db_path) as conn:
        for concept in dict.fromkeys(concept_names):
            result = conn.execute(
                """
                SELECT concept, code, name, relation_type, confidence, evidence, source
                FROM news_mapping_concept_stock_map
                WHERE concept = %s
                ORDER BY confidence DESC, code
                LIMIT %s
                """,
                (concept, int(max_stocks_per_concept)),
            ).fetchall()
            for row in result:
                item = dict(row)
                item["link_type"] = (
                    "industry_chain"
                    if item.get("relation_type") not in {"industry_member"}
                    else "macro_industry"
                )
                item["mapper"] = "concept_mapper"
                rows.append(item)

    return rows


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", type=str, default=str(NEWS_MAPPING_DB_PATH))
    parser.add_argument("--seed-default", action="store_true")
    parser.add_argument("--concept", type=str, default="")
    parser.add_argument("--limit", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    reports = {}

    if args.seed_default:
        reports["seed"] = seed_default_concept_stock_map(args.db_path)

    if args.concept:
        rows = map_concepts_to_stocks(
            [args.concept],
            db_path=args.db_path,
            max_stocks_per_concept=args.limit,
        )
        reports["mapping"] = rows

    if not reports:
        reports["seed"] = seed_default_concept_stock_map(args.db_path)

    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
