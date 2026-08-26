"""轮次摘要清单（ConversationInventory）：回答级复用的唯一入口。

职责分两侧：
- 沉淀侧 record_turn_summary：每轮 agent 回答落库后写入一条轮次摘要，
  作为后续轮次复用判断的数据源。
- 查询侧 build_conversation_inventory：为 decompose 构建候选清单，
  返回 LLM 判断面（items，进 prompt）与校验保护面（guards，仅服务端持有）。
- 精筛侧 load_candidate_answers：对 decompose 粗筛命中的候选去重截断并取
  完整回答裁剪版，供合并规划调用做内容级确认。

分工不变式：LLM 只看摘要做意图判断；数据内容与存在性由代码按 turn_id 回查。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent.artifacts import resolve_current_trade_date
from database.repositories import AgentRepository


# 清单配置常量
INVENTORY_VERSION = "conversation-inventory-v1"  # 清单结构版本号
DEFAULT_INVENTORY_LIMIT = 10  # 注入 prompt 的候选上限
DEFAULT_MAX_AGE_DAYS = 7  # 轮次级 TTL：超过该天数的摘要不进候选
SUMMARY_MAX_CHARS = 120  # 单行摘要最大字符数


def _now_text() -> str:
    # 当前时间文本（与 artifacts/session_state 保持同一格式）
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _one_line(text: str, max_chars: int = SUMMARY_MAX_CHARS) -> str:
    # 压缩为单行摘要：去换行与多余空白后截断（v1 确定性实现，后续可升级为 LLM 生成）
    compact = " ".join(str(text or "").split())
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars] + "..."


def previous_trade_date(current_trade_date: str) -> str:
    # 上一个交易日：对当前日期的前一天取最近交易日；失败时退化为自然日前一天
    try:
        from scheduler.trading_calendar import get_latest_trading_day

        current = datetime.strptime(str(current_trade_date), "%Y-%m-%d")
        return get_latest_trading_day(current - timedelta(days=1)).isoformat()
    except Exception:
        try:
            current = datetime.strptime(str(current_trade_date), "%Y-%m-%d")
            return (current - timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            return ""


def freshness_label(trade_date: str, current_trade_date: str) -> str:
    # 新鲜度标签（预计算，不让 LLM 自己算时间）：
    # current_trade_day 当日 / previous_trade_day 上一交易日 / older 更早 / non_trade 无交易日信息
    trade_date = str(trade_date or "")
    current_trade_date = str(current_trade_date or "")
    if not trade_date or not current_trade_date:
        return "non_trade"
    if trade_date == current_trade_date:
        return "current_trade_day"
    if trade_date == previous_trade_date(current_trade_date):
        return "previous_trade_day"
    return "older"


@dataclass(frozen=True)
class TurnSummaryItem:
    # 轮次摘要条目（沉淀侧写入结构）
    turn_id: str  # 轮次编号（真实 ID，仅服务端使用）
    user_id: str  # 用户 ID（复用作用域）
    conversation_id: str = ""  # 来源会话 ID（元数据，不做硬过滤）
    run_id: str = ""  # 关联的执行 run 编号
    trade_date: str = ""  # 所属交易日
    user_summary: str = ""  # 用户提问摘要（一行）
    assistant_summary: str = ""  # 助手回答摘要（一行）
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)  # 该轮产出 artifact 引用名单
    created_at: str = ""  # 生成时间

    def to_record(self) -> dict[str, Any]:
        # 转为 agent_turn_summaries 表记录
        return {
            "turn_id": self.turn_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "trade_date": self.trade_date,
            "user_summary": self.user_summary,
            "assistant_summary": self.assistant_summary,
            "artifact_refs": list(self.artifact_refs or []),
            "created_at": self.created_at or _now_text(),
            "metadata": {},
        }


def record_turn_summary(
    db_path: str | Path | None,
    *,
    user_id: str,
    conversation_id: str = "",
    run_id: str = "",
    user_message: str = "",
    assistant_answer: str = "",
    trade_date: str = "",
    artifact_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # 沉淀侧：写入一条轮次摘要，返回落库记录
    item = TurnSummaryItem(
        turn_id=f"turn_{uuid4().hex[:12]}",
        user_id=str(user_id or "default"),
        conversation_id=str(conversation_id or ""),
        run_id=str(run_id or ""),
        trade_date=str(trade_date or "") or resolve_current_trade_date(),
        user_summary=_one_line(user_message),
        assistant_summary=_one_line(assistant_answer),
        artifact_refs=list(artifact_refs or []),
        created_at=_now_text(),
    )
    record = item.to_record()
    AgentRepository(db_path).upsert_turn_summary(record)
    return record


def build_conversation_inventory(
    db_path: str | Path | None,
    *,
    user_id: str,
    current_trade_date: str = "",
    limit: int = DEFAULT_INVENTORY_LIMIT,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict[str, Any]:
    # 查询侧：构建 ConversationInventory
    # 返回结构：
    #   inventory_version → 清单结构版本号
    #   generated_at → 生成时间（实时查询，非快照）
    #   current_trade_date → 当前交易日
    #   items → LLM 判断面列表（显示短编号/两行摘要/新鲜度标签，进 prompt）
    #   guards → 校验保护面（按显示短编号映射真实 turn_id 与轮次级元数据，仅服务端持有）
    #   truncated → 是否因上限截断
    current_trade_date = str(current_trade_date or "") or resolve_current_trade_date()
    repo = AgentRepository(db_path)
    rows = repo.list_turn_summaries(str(user_id or ""), limit=max(limit * 3, 30))

    now = datetime.now()
    min_created = now - timedelta(days=max(1, int(max_age_days)))
    fresh_rows: list[dict[str, Any]] = []
    for row in rows:
        created_text = str(row.get("created_at") or "")
        try:
            created_at = datetime.strptime(created_text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue  # 时间格式异常的条目不进候选
        if created_at < min_created:
            continue  # 轮次级 TTL 保护：过期摘要不进候选
        fresh_rows.append(row)

    truncated = len(fresh_rows) > limit
    fresh_rows = fresh_rows[: max(1, int(limit))]

    items: list[dict[str, Any]] = []
    guards: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(fresh_rows, start=1):
        display_id = f"turn_{index:02d}"  # 显示短编号（防真实标识暴露给 LLM）
        trade_date = str(row.get("trade_date") or "")
        items.append(
            {
                "turn_id": display_id,  # LLM 引用的编号（粗筛/精判都填它）
                "user_summary": str(row.get("user_summary") or ""),
                "assistant_summary": str(row.get("assistant_summary") or ""),
                "freshness": freshness_label(trade_date, current_trade_date),
            }
        )
        guards[display_id] = {
            "turn_id": str(row.get("turn_id") or ""),  # 真实轮次编号（回查数据用）
            "conversation_id": str(row.get("conversation_id") or ""),
            "run_id": str(row.get("run_id") or ""),
            "trade_date": trade_date,
            "created_at": str(row.get("created_at") or ""),
            "artifact_refs": list(row.get("artifact_refs_json") or row.get("artifact_refs") or []),
        }

    return {
        "inventory_version": INVENTORY_VERSION,
        "generated_at": _now_text(),
        "current_trade_date": current_trade_date,
        "items": items,
        "guards": guards,
        "truncated": truncated,
    }


def render_inventory_lines(inventory: dict[str, Any]) -> list[str]:
    # 渲染为紧凑文本行（每条约 120-180 字符，供 decompose prompt 注入）
    lines: list[str] = []
    for item in list((inventory or {}).get("items") or []):
        lines.append(
            "- {turn_id} [{freshness}] 用户: {user_summary} | 回答: {assistant_summary}".format(
                turn_id=str(item.get("turn_id") or ""),
                freshness=str(item.get("freshness") or "non_trade"),
                user_summary=str(item.get("user_summary") or ""),
                assistant_summary=str(item.get("assistant_summary") or ""),
            )
        )
    return lines


def load_candidate_answers(
    db_path: str | Path | None,
    *,
    guards: dict[str, Any],
    display_ids: list[str],
    max_chars: int = 800,
    max_candidates: int = 3,
) -> list[dict[str, Any]]:
    # 规则层 1 候选精筛（纯代码）：
    # 按 display_ids 顺序处理（清单已按时间倒序，新的在前），
    # 内容去重留最新、截断 max_candidates 条，取完整回答裁剪版供合并规划调用精读。
    # 返回元素结构：
    #   turn_id → 显示短编号（LLM 引用它）
    #   answer_excerpt → 完整回答裁剪版
    #   trade_date → 该轮交易日（保护面元数据）
    #   artifact_refs → 该轮产出引用名单（数据级保护凭据）
    repo = AgentRepository(db_path)
    candidates: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    message_cache: dict[str, list[dict[str, Any]]] = {}
    for display_id in list(display_ids or []):
        guard = dict((guards or {}).get(str(display_id)) or {})
        if not guard:
            continue  # 引用不在校验面内 = 幻觉引用，直接丢弃
        conversation_id = str(guard.get("conversation_id") or "")
        run_id = str(guard.get("run_id") or "")
        if conversation_id not in message_cache:
            message_cache[conversation_id] = repo.list_messages(
                conversation_id,
                limit=100,
                descending=True,
            ) if conversation_id else []
        answer_text = ""
        for message in message_cache[conversation_id]:
            if str(message.get("role") or "") != "assistant":
                continue
            metadata = message.get("metadata_json") if isinstance(message.get("metadata_json"), dict) else {}
            if run_id and str(metadata.get("run_id") or "") != run_id:
                continue
            answer_text = str(message.get("content") or "")
            break
        if not answer_text.strip():
            continue  # 找不到完整回答 = 无货，不进候选
        content_hash = hashlib.sha1(answer_text.encode("utf-8")).hexdigest()
        if content_hash in seen_hashes:
            continue  # 同内容多轮只留最新一条
        seen_hashes.add(content_hash)
        candidates.append(
            {
                "turn_id": str(display_id),
                "answer_excerpt": answer_text[: max(200, int(max_chars))],
                "trade_date": str(guard.get("trade_date") or ""),
                "artifact_refs": list(guard.get("artifact_refs") or []),
            }
        )
        if len(candidates) >= max(1, int(max_candidates)):
            break
    return candidates
