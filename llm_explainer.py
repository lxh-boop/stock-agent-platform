from __future__ import annotations

from core.user_scope import owned_path

import json
from pathlib import Path
from typing import Any

import pandas as pd

from config import AI_EXPLANATION_DIR
from core.llm import LLMRuntimeSettings, LLMService
from llm_prompts import (
    DISCLAIMER_TEXT,
    FORBIDDEN_TERMS,
    build_stock_explanation_prompt as build_stock_explanation_messages,
    messages_to_text,
    validate_prompt_safety,
)


def _to_plain_dict(value: Any) -> dict:
    if value is None:
        return {}

    if isinstance(value, pd.Series):
        data = value.to_dict()
    elif isinstance(value, dict):
        data = dict(value)
    else:
        data = dict(pd.Series(value))

    out = {}
    for key, item in data.items():
        if pd.isna(item) if not isinstance(item, (list, dict, tuple)) else False:
            out[key] = None
        elif hasattr(item, "item"):
            try:
                out[key] = item.item()
            except Exception:
                out[key] = item
        else:
            out[key] = item
    return out


def _normalize_news_context(news_context: Any) -> list[dict]:
    if news_context is None:
        return []

    if isinstance(news_context, pd.DataFrame):
        return news_context.fillna("").to_dict(orient="records")

    if isinstance(news_context, list):
        return [
            _to_plain_dict(item) if not isinstance(item, dict) else dict(item)
            for item in news_context
        ]

    return [_to_plain_dict(news_context)]


def build_stock_prompt_messages(
    stock_row: dict | pd.Series,
    model_metrics: dict | None = None,
    risk_detail: dict | None = None,
    news_context: list[dict] | pd.DataFrame | None = None,
) -> list[dict]:
    return build_stock_explanation_messages(
        stock_row=_to_plain_dict(stock_row),
        model_metrics=model_metrics,
        risk_detail=risk_detail,
        news_context=_normalize_news_context(news_context),
    )


def build_stock_explanation_prompt(
    ranking_row: pd.Series | dict,
    rag_results: pd.DataFrame | None = None,
    model_metrics: dict | None = None,
    risk_detail: dict | None = None,
) -> str:
    messages = build_stock_prompt_messages(
        stock_row=ranking_row,
        model_metrics=model_metrics,
        risk_detail=risk_detail,
        news_context=rag_results,
    )
    return messages_to_text(messages)


def get_ai_explanation_path(stock_row: dict | pd.Series) -> Path:
    row = _to_plain_dict(stock_row)
    date_text = str(row.get("date", "unknown")).split(" ")[0]
    code_text = str(row.get("code", "unknown")).zfill(6)
    return owned_path(AI_EXPLANATION_DIR) / f"{date_text}_{code_text}.md"


def load_cached_ai_explanation(stock_row: dict | pd.Series) -> str:
    path = get_ai_explanation_path(stock_row)

    if not path.exists():
        return ""

    return path.read_text(encoding="utf-8", errors="ignore")


def save_ai_explanation(stock_row: dict | pd.Series, explanation: str) -> Path:
    path = get_ai_explanation_path(stock_row)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(explanation or ""), encoding="utf-8")
    return path


def _finalize_explanation(stock_row: dict | pd.Series, explanation: str) -> str:
    explanation = str(explanation or "").strip()

    if DISCLAIMER_TEXT not in explanation:
        explanation = explanation.rstrip() + f"\n\n{DISCLAIMER_TEXT}"

    forbidden_hits = [term for term in FORBIDDEN_TERMS if term in explanation]

    if forbidden_hits:
        return (
            "AI 解释生成失败：模型输出包含不允许的投资建议型表达："
            + "、".join(forbidden_hits)
        )

    save_ai_explanation(stock_row, explanation)
    return explanation


def explain_prompt_with_llm(
    stock_row: dict | pd.Series,
    prompt_text: str,
    *,
    llm_settings: LLMRuntimeSettings | None = None,
    llm_service: LLMService | None = None,
) -> str:
    prompt_text = str(prompt_text or "").strip()

    if not prompt_text:
        return "AI 解释生成失败：Prompt 为空，请先生成或填写 Prompt。"

    system_prompt = f"""
你是一个谨慎的量化模型解释助手，只能基于用户提供的 Prompt 做机器学习结果解释。
硬性规则：
1. 不给出买入、卖出、持有、加仓、减仓等交易建议。
2. 不输出目标价，不承诺收益，不暗示稳赚。
3. 必须保留免责声明：{DISCLAIMER_TEXT}
4. 只能使用“模型预测”“数据分析角度”“风险提示”“不确定性”“仅供研究展示”等表达。
""".strip()

    service = llm_service or (LLMService(llm_settings) if llm_settings is not None else None)
    if service is None or not service.is_available:
        return "AI 解释生成失败：当前 Model Profile 不可用。"

    try:
        explanation = service.generate_text(
            stage="report",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt_text},
            ],
            temperature=0.2,
            max_output_tokens=1200,
            operation="stock_prompt_explanation",
        )
    except Exception as exc:
        return f"AI 解释生成失败：{exc}"

    return _finalize_explanation(stock_row, explanation)


def explain_stock_with_llm(
    stock_row: dict,
    model_metrics: dict | None,
    risk_detail: dict | None,
    news_context: list[dict] | None,
    *,
    llm_settings: LLMRuntimeSettings | None = None,
    llm_service: LLMService | None = None,
) -> str:
    messages = build_stock_prompt_messages(
        stock_row=stock_row,
        model_metrics=model_metrics,
        risk_detail=risk_detail,
        news_context=news_context,
    )

    safety = validate_prompt_safety(messages)

    if not all(safety.values()):
        return (
            "AI 解释生成失败：Prompt 安全规则不完整。\n\n"
            f"```json\n{json.dumps(safety, ensure_ascii=False, indent=2)}\n```"
        )

    service = llm_service or (LLMService(llm_settings) if llm_settings is not None else None)
    if service is None or not service.is_available:
        return "AI 解释生成失败：当前 Model Profile 不可用。"

    try:
        explanation = service.generate_text(
            stage="report",
            messages=messages,
            temperature=0.2,
            max_output_tokens=1200,
            operation="stock_structured_explanation",
        )
    except Exception as exc:
        return f"AI 解释生成失败：{exc}"

    if DISCLAIMER_TEXT not in explanation:
        explanation = explanation.rstrip() + f"\n\n{DISCLAIMER_TEXT}"

    return _finalize_explanation(stock_row, explanation)


__all__ = [
    "build_stock_prompt_messages",
    "build_stock_explanation_prompt",
    "explain_prompt_with_llm",
    "explain_stock_with_llm",
    "get_ai_explanation_path",
    "load_cached_ai_explanation",
    "save_ai_explanation",
    "validate_prompt_safety",
]
