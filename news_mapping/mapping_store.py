from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .concept_mapper import map_concepts_to_stocks
from .entity_extractor import extract_entities_for_news_row
from .llm_mapper import load_news_item, map_news_with_llm
from .schema import NEWS_MAPPING_DB_PATH, execute_many, get_connection, init_db
from local_config import load_local_config


AUTO_CONFIRM_THRESHOLD = 0.85
PENDING_REVIEW_THRESHOLD = 0.60


def decide_status(confidence: float, direct_rule: bool = False) -> str | None:
    if direct_rule:
        return "auto_confirmed"
    if confidence >= AUTO_CONFIRM_THRESHOLD:
        return "auto_confirmed"
    if confidence >= PENDING_REVIEW_THRESHOLD:
        return "pending_review"
    return None


def normalize_code(value) -> str:
    return str(value or "").strip().split(".")[0].zfill(6)


def save_news_stock_links(
    news_id: str,
    links: list[dict],
    mapper: str,
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
) -> dict:
    init_db(db_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    skipped = 0

    for link in links:
        confidence = float(link.get("confidence", 0.0) or 0.0)
        direct_rule = bool(link.get("direct_rule", False))
        status = link.get("status") or decide_status(confidence, direct_rule=direct_rule)
        if status is None:
            skipped += 1
            continue

        evidence = str(link.get("evidence") or "").strip()
        if not evidence:
            skipped += 1
            continue

        rows.append(
            {
                "news_id": news_id,
                "code": normalize_code(link.get("code")),
                "name": str(link.get("name") or ""),
                "link_type": str(link.get("link_type") or "unknown"),
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": str(link.get("reason") or ""),
                "evidence": evidence,
                "mapper": mapper,
                "status": status,
                "created_at": now,
            }
        )

    if rows:
        with get_connection(db_path) as conn:
            execute_many(conn, 
                """
                INSERT INTO news_mapping_news_stock_links
                    (news_id, code, name, link_type, confidence, reason, evidence, mapper, status, created_at)
                VALUES
                    (%(news_id)s, %(code)s, %(name)s, %(link_type)s, %(confidence)s, %(reason)s, %(evidence)s, %(mapper)s, %(status)s, %(created_at)s)
                ON CONFLICT(news_id, code, mapper) DO UPDATE SET
                    name=excluded.name,
                    link_type=excluded.link_type,
                    confidence=excluded.confidence,
                    reason=excluded.reason,
                    evidence=excluded.evidence,
                    status=excluded.status,
                    created_at=excluded.created_at
                """,
                rows,
            )
            conn.commit()

    return {
        "mapper": mapper,
        "input_links": len(links),
        "saved_links": len(rows),
        "skipped_links": skipped,
    }


def links_from_rule_extract(rule_extract: dict) -> list[dict]:
    links = []
    for item in rule_extract.get("candidate_stocks", []):
        evidence = item.get("matched_text") or item.get("name") or item.get("code")
        links.append(
            {
                "code": item.get("code"),
                "name": item.get("name"),
                "link_type": "direct_company",
                "confidence": max(0.85, float(item.get("confidence", 0.0) or 0.0)),
                "reason": f"规则命中股票别名/代码：{evidence}",
                "evidence": str(evidence),
                "direct_rule": True,
            }
        )
    return links


def links_from_concept_mapping(concept_links: list[dict]) -> list[dict]:
    links = []
    for item in concept_links:
        links.append(
            {
                "code": item.get("code"),
                "name": item.get("name"),
                "link_type": item.get("link_type") or "concept",
                "confidence": float(item.get("confidence", 0.0) or 0.0),
                "reason": f"概念/行业候选：{item.get('concept')}",
                "evidence": item.get("evidence") or item.get("concept") or "",
            }
        )
    return links


def store_mapping_result(
    news_item: dict,
    rule_extract: dict | None = None,
    concept_links: list[dict] | None = None,
    llm_result: dict | None = None,
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
) -> dict:
    news_id = news_item["news_id"]
    rule_extract = rule_extract or extract_entities_for_news_row(news_item, db_path=db_path)
    if concept_links is None:
        concept_links = map_concepts_to_stocks(
            rule_extract.get("candidate_concepts", []),
            db_path=db_path,
            max_stocks_per_concept=30,
        )

    reports = []
    reports.append(
        save_news_stock_links(
            news_id=news_id,
            links=links_from_rule_extract(rule_extract),
            mapper="rule_alias",
            db_path=db_path,
        )
    )
    reports.append(
        save_news_stock_links(
            news_id=news_id,
            links=links_from_concept_mapping(concept_links),
            mapper="concept_mapper",
            db_path=db_path,
        )
    )
    if llm_result is not None:
        reports.append(
            save_news_stock_links(
                news_id=news_id,
                links=llm_result.get("links", []),
                mapper="llm_mapper",
                db_path=db_path,
            )
        )

    return {
        "news_id": news_id,
        "reports": reports,
        "total_saved": sum(item["saved_links"] for item in reports),
        "total_skipped": sum(item["skipped_links"] for item in reports),
    }


def update_link_status(
    news_id: str,
    code: str,
    mapper: str,
    new_status: str,
    comment: str = "",
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
) -> dict:
    if new_status not in {"auto_confirmed", "pending_review", "rejected", "manual_confirmed"}:
        raise ValueError(f"非法 status: {new_status}")

    code = normalize_code(code)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT status FROM news_mapping_news_stock_links
            WHERE news_id=%s AND code=%s AND mapper=%s
            """,
            (news_id, code, mapper),
        ).fetchone()
        old_status = row["status"] if row else ""
        conn.execute(
            """
            UPDATE news_mapping_news_stock_links
            SET status=%s
            WHERE news_id=%s AND code=%s AND mapper=%s
            """,
            (new_status, news_id, code, mapper),
        )
        conn.execute(
            """
            INSERT INTO news_mapping_mapping_feedback
                (news_id, code, old_status, new_status, comment, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (news_id, code, old_status, new_status, comment, now),
        )
        conn.commit()

    return {
        "news_id": news_id,
        "code": code,
        "mapper": mapper,
        "old_status": old_status,
        "new_status": new_status,
    }


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--news-id", type=str, required=True)
    parser.add_argument("--db-path", type=str, default=str(NEWS_MAPPING_DB_PATH))
    parser.add_argument("--with-llm", action="store_true")
    parser.add_argument("--use-local-config-ai", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    news_item = load_news_item(args.news_id, db_path=args.db_path)
    rule_extract = extract_entities_for_news_row(news_item, db_path=args.db_path)
    concept_links = map_concepts_to_stocks(
        rule_extract.get("candidate_concepts", []),
        db_path=args.db_path,
        max_stocks_per_concept=30,
    )
    llm_result = None
    if args.with_llm:
        cfg = load_local_config() if args.use_local_config_ai else {}
        api_key = str(cfg.get("llm_api_key") or "")
        if not api_key:
            raise RuntimeError("with-llm 需要 --use-local-config-ai 或后续扩展显式 API 参数。")
        llm_result = map_news_with_llm(
            news_item=news_item,
            api_key=api_key,
            base_url=str(cfg.get("llm_base_url") or ""),
            model=str(cfg.get("llm_model") or ""),
            db_path=args.db_path,
            rule_extract=rule_extract,
            concept_links=concept_links,
        )

    report = store_mapping_result(
        news_item=news_item,
        rule_extract=rule_extract,
        concept_links=concept_links,
        llm_result=llm_result,
        db_path=args.db_path,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
