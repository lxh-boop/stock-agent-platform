from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd

from llm_client import LLMClient
from local_config import load_local_config
from .concept_mapper import map_concepts_to_stocks
from .entity_extractor import extract_entities_for_news_row
from .schema import NEWS_MAPPING_DB_PATH, get_connection, init_db


ALLOWED_LINK_TYPES = {
    "direct_company",
    "subsidiary",
    "industry_chain",
    "concept",
    "competitor",
    "macro_industry",
    "region",
    "unknown",
}


def compact_candidates(items: list[dict], limit: int = 30) -> list[dict]:
    compacted = []
    for item in items[:limit]:
        compacted.append(
            {
                k: item.get(k)
                for k in [
                    "code",
                    "name",
                    "matched_text",
                    "match_type",
                    "concept",
                    "relation_type",
                    "link_type",
                    "confidence",
                    "evidence",
                ]
                if k in item
            }
        )
    return compacted


def build_llm_mapping_messages(
    news_item: dict,
    rule_extract: dict,
    concept_links: list[dict],
) -> list[dict]:
    payload = {
        "news": {
            "news_id": news_item.get("news_id"),
            "date": news_item.get("date"),
            "title": news_item.get("title"),
            "content": news_item.get("content"),
            "source": news_item.get("source"),
        },
        "rule_candidate_stocks": compact_candidates(rule_extract.get("candidate_stocks", []), limit=30),
        "rule_candidate_concepts": compact_candidates(rule_extract.get("candidate_concepts", []), limit=30),
        "concept_candidate_stocks": compact_candidates(concept_links, limit=50),
    }

    system_prompt = """
你是金融新闻到股票映射助手，只负责判断新闻文本与上市公司之间是否存在有证据的关系。
硬性规则：
1. 不允许凭空关联股票。
2. 每个 links 项必须给出 evidence，且 evidence 必须来自新闻标题、正文或输入候选。
3. 没有证据时返回空 links。
4. confidence 必须在 0 到 1。
5. 直接公司名/股票代码命中可以给较高置信度。
6. 概念、行业、产业链映射置信度应低于直接公司命中。
7. 宏观新闻不能强行映射到大量股票。
8. 只输出严格 JSON，不要输出 Markdown，不要解释 JSON 之外的文字。
9. 不提供买入、卖出、目标价、收益承诺或实盘交易建议。
""".strip()

    user_prompt = f"""
请根据输入，返回严格 JSON：

{{
  "links": [
    {{
      "code": "600111",
      "name": "北方稀土",
      "link_type": "industry_chain",
      "confidence": 0.82,
      "reason": "新闻涉及稀土价格上涨，公司属于稀土产业链候选，存在相关性但不是直接公司公告",
      "evidence": "新闻中出现“稀土价格上涨”"
    }}
  ],
  "new_concepts": [
    {{
      "concept": "稀土永磁",
      "reason": "新闻多次涉及稀土永磁材料"
    }}
  ],
  "new_alias_candidates": [
    {{
      "alias": "某简称",
      "code": "xxxxxx",
      "confidence": 0.75,
      "reason": "新闻中该简称指向该上市公司"
    }}
  ]
}}

输入：
{json.dumps(payload, ensure_ascii=False, indent=2)}
""".strip()

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def parse_json_response(text: str) -> dict:
    text = str(text or "").strip()
    if not text:
        return {"links": [], "new_concepts": [], "new_alias_candidates": []}

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
    if fenced:
        text = fenced.group(1).strip()

    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        data = json.loads(match.group(0))

    if not isinstance(data, dict):
        raise ValueError("LLM JSON 顶层必须是对象。")

    data.setdefault("links", [])
    data.setdefault("new_concepts", [])
    data.setdefault("new_alias_candidates", [])
    return data


def sanitize_llm_mapping(data: dict) -> dict:
    clean_links = []
    for link in data.get("links", []) or []:
        if not isinstance(link, dict):
            continue
        code = str(link.get("code") or "").strip()
        code = code.split(".")[0].zfill(6) if code else ""
        name = str(link.get("name") or "").strip()
        evidence = str(link.get("evidence") or "").strip()
        reason = str(link.get("reason") or "").strip()
        if not code or not name or not evidence:
            continue
        try:
            confidence = float(link.get("confidence", 0))
        except Exception:
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        link_type = str(link.get("link_type") or "unknown").strip()
        if link_type not in ALLOWED_LINK_TYPES:
            link_type = "unknown"
        clean_links.append(
            {
                "code": code,
                "name": name,
                "link_type": link_type,
                "confidence": confidence,
                "reason": reason,
                "evidence": evidence,
            }
        )

    clean_concepts = []
    for item in data.get("new_concepts", []) or []:
        if isinstance(item, dict) and str(item.get("concept") or "").strip():
            clean_concepts.append(
                {
                    "concept": str(item.get("concept")).strip(),
                    "reason": str(item.get("reason") or "").strip(),
                }
            )

    clean_aliases = []
    for item in data.get("new_alias_candidates", []) or []:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("alias") or "").strip()
        code = str(item.get("code") or "").strip()
        if not alias or not code:
            continue
        try:
            confidence = float(item.get("confidence", 0))
        except Exception:
            confidence = 0.0
        clean_aliases.append(
            {
                "alias": alias,
                "code": code.split(".")[0].zfill(6),
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": str(item.get("reason") or "").strip(),
            }
        )

    return {
        "links": clean_links,
        "new_concepts": clean_concepts,
        "new_alias_candidates": clean_aliases,
    }


def map_news_with_llm(
    news_item: dict,
    api_key: str,
    base_url: str = "",
    model: str = "",
    db_path: str | Path = NEWS_MAPPING_DB_PATH,
    rule_extract: dict | None = None,
    concept_links: list[dict] | None = None,
) -> dict:
    rule_extract = rule_extract or extract_entities_for_news_row(news_item, db_path=db_path)
    if concept_links is None:
        concept_links = map_concepts_to_stocks(
            rule_extract.get("candidate_concepts", []),
            db_path=db_path,
            max_stocks_per_concept=30,
        )

    messages = build_llm_mapping_messages(
        news_item=news_item,
        rule_extract=rule_extract,
        concept_links=concept_links,
    )

    client = LLMClient(api_key=api_key, base_url=base_url, model=model)
    raw_text = client.chat(messages=messages, temperature=0.0, max_tokens=1200)
    parsed = sanitize_llm_mapping(parse_json_response(raw_text))
    parsed["raw_response"] = raw_text
    return parsed


def load_news_item(news_id: str, db_path: str | Path = NEWS_MAPPING_DB_PATH) -> dict:
    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT news_id, date, publish_time, title, content, source, url
            FROM news_mapping_news_items
            WHERE news_id = %s
            """,
            (news_id,),
        ).fetchone()
    if row is None:
        raise FileNotFoundError(f"news_id 不存在：{news_id}")
    return dict(row)


def load_latest_news_item(db_path: str | Path = NEWS_MAPPING_DB_PATH) -> dict:
    init_db(db_path)
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT news_id, date, publish_time, title, content, source, url
            FROM news_mapping_news_items
            ORDER BY publish_time DESC
            LIMIT 1
            """
        ).fetchone()
    if row is None:
        raise FileNotFoundError("news_items 为空，请先导入新闻。")
    return dict(row)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--news-id", type=str, default="")
    parser.add_argument("--db-path", type=str, default=str(NEWS_MAPPING_DB_PATH))
    parser.add_argument("--api-key", type=str, default="")
    parser.add_argument("--base-url", type=str, default="")
    parser.add_argument("--model", type=str, default="")
    parser.add_argument("--use-local-config-ai", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    news_item = (
        load_news_item(args.news_id, db_path=args.db_path)
        if args.news_id
        else load_latest_news_item(db_path=args.db_path)
    )
    rule_extract = extract_entities_for_news_row(news_item, db_path=args.db_path)
    concept_links = map_concepts_to_stocks(
        rule_extract.get("candidate_concepts", []),
        db_path=args.db_path,
        max_stocks_per_concept=10,
    )

    if args.dry_run:
        messages = build_llm_mapping_messages(news_item, rule_extract, concept_links)
        print(json.dumps({"news_item": news_item, "messages": messages}, ensure_ascii=False, indent=2))
        return

    api_key = args.api_key
    base_url = args.base_url
    model = args.model
    if args.use_local_config_ai:
        cfg = load_local_config()
        api_key = api_key or str(cfg.get("llm_api_key") or "")
        base_url = base_url or str(cfg.get("llm_base_url") or "")
        model = model or str(cfg.get("llm_model") or "")

    if not api_key:
        raise RuntimeError("调用 LLM 映射器需要 API Key。")

    result = map_news_with_llm(
        news_item=news_item,
        api_key=api_key,
        base_url=base_url,
        model=model,
        db_path=args.db_path,
        rule_extract=rule_extract,
        concept_links=concept_links,
    )
    print(json.dumps({k: v for k, v in result.items() if k != "raw_response"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
