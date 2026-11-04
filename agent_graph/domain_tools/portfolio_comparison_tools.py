from __future__ import annotations

import json
import math
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_graph.trace import flow_event, trace_event, trace_exception
from core.llm import LLMService
from core.llm.dependencies import get_llm_execution_dependencies


_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_id(value: Any, default: str) -> str:
    text = _SAFE_ID.sub("_", str(value or "").strip())[:120]
    return text or default


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _unwrap(value: Any) -> Any:
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            break
        data = current.get("data")
        if isinstance(data, dict) and len(current) <= 16:
            current = data
            continue
        break
    return current


_PRIORITY_NESTED_KEYS = (
    "constraints",
    "risk_limits",
    "limits",
    "risk_assessment",
    "strategy_constraints",
    "profile",
    "user_profile",
    "risk_report",
    "portfolio_state",
    "account_summary",
    "account",
    "summary",
    "data",
    "result",
    "payload",
)

_CONSTRAINT_ALIASES = {
    "max_single_weight": (
        "max_single_weight",
        "max_single_position",
        "single_position_limit",
        "max_position_weight",
        "single_stock_limit",
        "user_max_single_position",
        "max_single_limit",
    ),
    "max_industry_weight": (
        "max_industry_weight",
        "max_industry_position",
        "max_industry_exposure",
        "industry_limit",
        "industry_position_limit",
        "user_max_industry_exposure",
        "max_industry_limit",
    ),
}


def _find_first(
    value: Any,
    keys: tuple[str, ...],
    *,
    depth: int = 0,
    visited: set[int] | None = None,
) -> Any:
    """Find the first non-empty value across heterogeneous tool envelopes.

    Upstream tools use several compatible shapes (``data.constraints``,
    ``user_profile.constraints``, ``risk_assessment`` and flattened summaries).
    The old implementation only followed a short hard-coded path and therefore
    treated existing values as missing. This resolver searches mappings in a
    deterministic priority order, with cycle and depth protection.
    """

    if depth > 8 or value is None:
        return None

    if visited is None:
        visited = set()
    if isinstance(value, (dict, list, tuple)):
        marker = id(value)
        if marker in visited:
            return None
        visited.add(marker)

    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key] not in (None, "", [], {}):
                return value[key]

        ordered_keys = [
            key for key in _PRIORITY_NESTED_KEYS
            if key in value
        ]
        ordered_keys.extend(
            key for key in value
            if key not in ordered_keys
        )
        for nested_key in ordered_keys:
            nested = value.get(nested_key)
            if not isinstance(nested, (dict, list, tuple)):
                continue
            found = _find_first(
                nested,
                keys,
                depth=depth + 1,
                visited=visited,
            )
            if found not in (None, "", [], {}):
                return found

    elif isinstance(value, (list, tuple)):
        for nested in value[:100]:
            found = _find_first(
                nested,
                keys,
                depth=depth + 1,
                visited=visited,
            )
            if found not in (None, "", [], {}):
                return found

    return None


def _resolve_risk_constraints(
    *,
    user_profile: Any,
    risk_report: Any,
    explicit_single: Any = None,
    explicit_industry: Any = None,
) -> tuple[dict[str, float | None], dict[str, str]]:
    """Normalize risk limits and preserve their source provenance."""

    values: dict[str, float | None] = {
        "max_single_weight": None,
        "max_industry_weight": None,
    }
    sources: dict[str, str] = {}

    explicit = {
        "max_single_weight": explicit_single,
        "max_industry_weight": explicit_industry,
    }
    for canonical, raw in explicit.items():
        parsed = _ratio(raw)
        if parsed is not None:
            values[canonical] = parsed
            sources[canonical] = "explicit_task_parameter"

    for canonical, aliases in _CONSTRAINT_ALIASES.items():
        if values[canonical] is not None:
            continue
        raw = _find_first(user_profile, aliases)
        parsed = _ratio(raw)
        if parsed is not None:
            values[canonical] = parsed
            sources[canonical] = "user_profile"

    for canonical, aliases in _CONSTRAINT_ALIASES.items():
        if values[canonical] is not None:
            continue
        raw = _find_first(risk_report, aliases)
        parsed = _ratio(raw)
        if parsed is not None:
            values[canonical] = parsed
            sources[canonical] = "risk_report"

    return values, sources


def _list_first(value: Any, keys: tuple[str, ...]) -> list[Any]:
    raw = _find_first(value, keys)
    return list(raw) if isinstance(raw, list) else []


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _ratio(value: Any, default: float | None = None) -> float | None:
    number = _float(value, default)
    if number is None:
        return None
    if number > 1.0 and number <= 100.0:
        number /= 100.0
    return max(0.0, min(number, 1.0))


def _code(value: Any) -> str:
    """Return a structured security identifier without market-prefix rules.

    This function is only called for fields already named ``stock_code``,
    ``code`` or ``ts_code``.  It deliberately does not infer business meaning
    from an arbitrary six-digit number and does not restrict identifiers to a
    hard-coded exchange prefix table.
    """

    text = str(value or "").strip().upper()
    return text[:64] if text else ""


def _position_rows(value: Any) -> list[dict[str, Any]]:
    payload = _unwrap(value)
    rows = _list_first(payload, ("positions", "holdings", "current_positions"))
    result: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        code = _code(raw.get("stock_code") or raw.get("code") or raw.get("ts_code"))
        if not code:
            continue
        result.append(
            {
                "stock_code": code,
                "stock_name": str(raw.get("stock_name") or raw.get("name") or "").strip(),
                "industry": str(raw.get("industry") or raw.get("industry_name") or "").strip(),
                "quantity": _float(raw.get("quantity") or raw.get("shares"), 0.0) or 0.0,
                "current_price": _float(raw.get("current_price") or raw.get("price"), 0.0) or 0.0,
                "current_weight": _ratio(raw.get("position_ratio") if raw.get("position_ratio") is not None else raw.get("weight"), 0.0) or 0.0,
            }
        )
    return result


def _ranking_rows(value: Any) -> list[dict[str, Any]]:
    """Build a compact candidate universe from the ranking tool result.

    No candidate is selected here.  Every row returned by the upstream ranking
    task remains available to the LLM.  The LLM decides which securities are
    relevant to the current user goal.
    """

    payload = _unwrap(value)
    rows = _list_first(payload, ("records", "rows", "items", "ranking", "candidate_stocks"))
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            continue
        code = _code(raw.get("stock_code") or raw.get("code") or raw.get("ts_code") or raw.get("symbol"))
        if not code:
            continue
        rank = raw.get("rank") or raw.get("ranking") or raw.get("original_rank") or index
        try:
            rank_value: int | str = int(rank)
        except (TypeError, ValueError):
            rank_value = str(rank or index)
        score = _float(
            raw.get("final_score")
            if raw.get("final_score") is not None
            else raw.get("score")
            if raw.get("score") is not None
            else raw.get("prediction"),
            0.0,
        ) or 0.0
        result.append(
            {
                "stock_code": code,
                "stock_name": str(raw.get("stock_name") or raw.get("name") or "").strip(),
                "industry": str(raw.get("industry") or raw.get("industry_name") or "").strip(),
                "rank": rank_value,
                "score": score,
                "predicted_return": _float(
                    raw.get("pred_5d_ret")
                    if raw.get("pred_5d_ret") is not None
                    else raw.get("prediction")
                    if raw.get("prediction") is not None
                    else raw.get("raw_score"),
                    None,
                ),
                "risk_score": _float(raw.get("risk_score"), None),
                "risk_level": str(raw.get("risk_level") or "").strip(),
                "confidence_score": _float(raw.get("confidence_score"), None),
                "confidence": str(raw.get("confidence") or raw.get("confidence_level") or "").strip(),
                "volatility": _float(
                    raw.get("vol_20") if raw.get("vol_20") is not None else raw.get("volatility"),
                    None,
                ),
                "drawdown": _float(
                    raw.get("drawdown_20") if raw.get("drawdown_20") is not None else raw.get("drawdown"),
                    None,
                ),
                "current_price": _float(raw.get("current_price") or raw.get("price") or raw.get("close"), 0.0) or 0.0,
            }
        )
    return result


def _profile_constraint(profile: Any, keys: tuple[str, ...]) -> float | None:
    payload = _unwrap(profile)
    value = _find_first(payload, keys)
    return _ratio(value)


def _account_cash_ratio(value: Any) -> float:
    payload = _unwrap(value)
    direct = _ratio(_find_first(payload, ("cash_ratio", "cash_weight")))
    if direct is not None:
        return direct
    cash = _float(_find_first(payload, ("cash", "available_cash", "cash_balance")), 0.0) or 0.0
    total = _float(_find_first(payload, ("total_assets", "total_asset", "nav")), 0.0) or 0.0
    return max(0.0, min(cash / total, 1.0)) if total > 0 else 0.0


def _account_total_assets(value: Any) -> float | None:
    payload = _unwrap(value)
    return _float(
        _find_first(
            payload,
            (
                "total_assets",
                "total_asset",
                "account_value",
                "portfolio_value",
                "net_asset_value",
                "nav_amount",
            ),
        ),
        None,
    )


def _industry_metadata_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    known = sum(
        1
        for row in rows
        if str(row.get("industry") or "").strip()
    )
    return {
        "row_count": total,
        "known_industry_count": known,
        "unknown_industry_count": max(0, total - known),
        "coverage": round(known / total, 8) if total else 0.0,
        "verifiable": bool(total and known == total),
    }


def _industry_exposure(
    rows: list[dict[str, Any]],
    weight_key: str,
    *,
    include_unknown: bool = False,
) -> dict[str, float]:
    exposure: dict[str, float] = {}
    for row in rows:
        industry = str(row.get("industry") or "").strip()
        if not industry:
            if not include_unknown:
                continue
            industry = "未知行业"
        weight = _ratio(row.get(weight_key), 0.0) or 0.0
        exposure[industry] = exposure.get(industry, 0.0) + weight
    return {
        key: round(value, 8)
        for key, value in sorted(exposure.items())
    }


def _risk_snapshot(
    rows: list[dict[str, Any]],
    weight_key: str,
    cash_weight: float,
) -> dict[str, Any]:
    weights = [
        _ratio(row.get(weight_key), 0.0) or 0.0
        for row in rows
    ]
    metadata = _industry_metadata_status(rows)
    known_industry = _industry_exposure(
        rows,
        weight_key,
        include_unknown=False,
    )
    unknown_weight = round(
        sum(
            _ratio(row.get(weight_key), 0.0) or 0.0
            for row in rows
            if not str(row.get("industry") or "").strip()
        ),
        8,
    )
    max_industry = (
        round(max(known_industry.values(), default=0.0), 8)
        if metadata["verifiable"]
        else None
    )
    return {
        "position_count": len(
            [weight for weight in weights if weight > 0]
        ),
        "cash_weight": round(cash_weight, 8),
        "invested_weight": round(sum(weights), 8),
        "max_single_weight": round(max(weights, default=0.0), 8),
        "concentration_hhi": round(
            sum(weight * weight for weight in weights),
            8,
        ),
        "max_industry_weight": max_industry,
        "industry_exposure": known_industry,
        "unknown_industry_weight": unknown_weight,
        "industry_metadata": metadata,
        "industry_constraint_verifiable": bool(
            metadata["verifiable"]
        ),
    }


def _clean_design_explanations(
    design: dict[str, Any],
    *,
    target_position_count: int,
    target_cash_weight: float,
) -> None:
    """Keep LLM business reasoning while making accounting facts authoritative.

    Previous portfolio limits and user-profile limits are reference context only.
    This function does not turn them into constraints for the new strategy.
    """

    original = [
        str(item).strip()
        for item in (design.get("design_rationale") or [])
        if str(item).strip()
    ]
    summary = (
        f"LLM 本轮自主设计 {target_position_count} 只证券，最终会计现金权重为 "
        f"{target_cash_weight:.2%}。旧持仓、旧策略参数和用户画像仅作为参考信息，"
        "不会由构造器强制继承。"
    )
    design["design_rationale"] = list(dict.fromkeys([summary, *original]))[:16]

    assumptions = [
        str(item).strip()
        for item in (design.get("assumptions") or [])
        if str(item).strip()
    ]
    assumptions.append("证券选择、目标权重和新策略参数均由 LLM 根据本轮用户问题决定。")
    assumptions.append("确定性阶段仅验证真实证券来源、非负权重和总权重会计平衡，并计算金额与股数。")
    assumptions.append("当前持仓、风险报告和历史策略限制只用于对比，不会覆盖 LLM 的新策略。")
    design["assumptions"] = list(dict.fromkeys(assumptions))[:16]

    source_map = dict(design.get("source_map") or {})
    source_map.update(
        {
            "selected_candidates": "LLM 根据当前用户目标和真实上游数据自主决定",
            "target_weights": "LLM 输出；确定性阶段仅做会计归一化，不应用旧策略上限",
            "strategy_parameters": "LLM 为本轮新策略自主提出",
            "previous_constraints": "仅作历史参考和结果对比，不强制应用",
        }
    )
    design["source_map"] = source_map


def _classify_design_limitations(
    items: list[str],
    *,
    total_assets: float | None,
) -> tuple[list[str], list[str]]:
    """Separate real system limitations from already-resolved claims."""

    limitations: list[str] = []
    resolved_claims: list[str] = []
    for raw in items:
        text = str(raw or "").strip()
        if not text:
            continue
        lowered = text.lower()
        mentions_assets = any(
            marker in lowered
            for marker in (
                "total_assets",
                "total asset",
                "总资产",
                "资产规模",
                "账户规模",
            )
        )
        if mentions_assets and total_assets is not None:
            resolved_claims.append(text)
            continue
        limitations.append(text)
    return (
        list(dict.fromkeys(limitations)),
        list(dict.fromkeys(resolved_claims)),
    )


class TargetPortfolioStore:
    """Small conversation-scoped domain artifact store.

    Generic ToolExecutor artifacts are still created. This store additionally makes
    a target portfolio addressable in a later conversation turn without exposing a
    local path to the LLM or UI.
    """

    def __init__(self, output_dir: str | Path = "outputs") -> None:
        self.root = Path(output_dir) / "target_portfolio_artifacts"

    def _conversation_dir(self, user_id: str, conversation_id: str) -> Path:
        return self.root / _safe_id(user_id, "default") / _safe_id(conversation_id, "no_conversation")

    def save(
        self,
        payload: dict[str, Any],
        *,
        user_id: str,
        conversation_id: str,
        run_id: str = "",
        task_id: str = "",
    ) -> dict[str, Any]:
        artifact_id = f"target_portfolio_{uuid.uuid4().hex}"
        directory = self._conversation_dir(user_id, conversation_id)
        directory.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now().isoformat(timespec="seconds")
        record = {
            "artifact_id": artifact_id,
            "artifact_type": "target_portfolio",
            "user_id": str(user_id or "default"),
            "conversation_id": str(conversation_id or ""),
            "run_id": str(run_id or ""),
            "task_id": str(task_id or ""),
            "created_at": created_at,
            "payload": payload,
        }
        target = directory / f"{artifact_id}.json"
        fd, tmp_name = tempfile.mkstemp(prefix="target_portfolio_", suffix=".tmp", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return {
            "artifact_id": artifact_id,
            "artifact_type": "target_portfolio",
            "created_at": created_at,
            "conversation_id": str(conversation_id or ""),
            "run_id": str(run_id or ""),
            "task_id": str(task_id or ""),
        }

    def list_refs(self, *, user_id: str, conversation_id: str) -> list[dict[str, Any]]:
        directory = self._conversation_dir(user_id, conversation_id)
        if not directory.exists():
            return []
        refs: list[dict[str, Any]] = []
        for path in directory.glob("target_portfolio_*.json"):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    record = json.load(handle)
                refs.append(
                    {
                        "artifact_id": str(record.get("artifact_id") or path.stem),
                        "artifact_type": "target_portfolio",
                        "created_at": str(record.get("created_at") or ""),
                        "run_id": str(record.get("run_id") or ""),
                        "task_id": str(record.get("task_id") or ""),
                    }
                )
            except Exception:
                continue
        refs.sort(key=lambda item: (item.get("created_at") or "", item.get("artifact_id") or ""), reverse=True)
        return refs

    def load(
        self,
        *,
        user_id: str,
        conversation_id: str,
        artifact_id: str = "",
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        refs = self.list_refs(user_id=user_id, conversation_id=conversation_id)
        if artifact_id:
            wanted = _safe_id(artifact_id, "")
            matching = [item for item in refs if item.get("artifact_id") == wanted]
            if not matching:
                return None, refs
            selected = matching[0]
        else:
            if len(refs) != 1:
                return None, refs
            selected = refs[0]
        path = self._conversation_dir(user_id, conversation_id) / f"{selected['artifact_id']}.json"
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
            return dict(record or {}), refs
        except Exception:
            return None, refs


def _missing_result(message: str, missing: list[str], *, question: str) -> dict[str, Any]:
    return {
        "success": False,
        "status": "missing_required_parameters",
        "message": message,
        "errors": ["missing_required_parameters"],
        "data": {
            "need_clarification": True,
            "missing_parameters": missing,
            "clarification_question": question,
            "not_executed": True,
        },
    }


def _system_data_missing_result(
    message: str,
    missing: list[str],
    *,
    next_action: str = "report_limitation",
) -> dict[str, Any]:
    """Represent system-owned data gaps without asking the user to supply them."""

    return {
        "success": False,
        "status": "system_data_missing",
        "message": message,
        "errors": ["system_data_missing"],
        "data": {
            "need_clarification": False,
            "missing_system_data": list(missing),
            "replan_required": next_action == "replan_readonly",
            "next_action": next_action,
            "not_executed": True,
        },
    }


def _runtime_llm_service(context: dict[str, Any]) -> LLMService | None:
    """Resolve the Executor-owned service from the private process registry."""

    dependencies = get_llm_execution_dependencies(str(context.get("run_id") or ""))
    return dependencies.llm_service if dependencies is not None else None


def _profile_summary(value: Any) -> dict[str, Any]:
    payload = _unwrap(value)
    if not isinstance(payload, dict):
        return {}
    allowed = {
        "risk_level", "risk_preference", "risk_tolerance", "profile_type",
        "investment_goal", "investment_horizon", "max_single_position",
        "max_single_weight", "single_position_limit", "max_industry_exposure",
        "max_industry_weight", "max_industry_position", "industry_limit", "target_position_count",
        "preferred_position_count", "target_cash_weight", "cash_weight",
        "min_cash_weight", "max_cash_weight", "turnover_preference",
        "strategy_name", "allocation_method", "candidate_policy",
    }
    result: dict[str, Any] = {}
    for key, item in payload.items():
        if str(key) in allowed and item not in (None, "", [], {}):
            result[str(key)] = item
    nested = payload.get("constraints")
    if isinstance(nested, dict):
        for key, item in nested.items():
            if str(key) in allowed and item not in (None, "", [], {}):
                result[str(key)] = item
    return result


def _risk_summary(value: Any) -> dict[str, Any]:
    payload = _unwrap(value)
    if not isinstance(payload, dict):
        return {}
    report = payload.get("risk_report") if isinstance(payload.get("risk_report"), dict) else payload
    keys = (
        "risk_level", "risk_score", "position_count", "cash_ratio",
        "cash_weight", "max_single_position", "max_single_weight",
        "largest_position_weight", "max_industry_exposure",
        "max_industry_weight", "max_industry_position", "industry_concentration",
        "concentration_hhi", "warnings", "risk_warnings", "violations",
    )
    return {key: report.get(key) for key in keys if report.get(key) not in (None, "", [], {})}


def _compact_user_goal(value: Any) -> dict[str, Any]:
    payload = _unwrap(value)
    if not isinstance(payload, dict):
        return {}
    allowed = (
        "raw_message",
        "resolved_message",
        "goal_summary",
        "action",
        "objects",
        "constraints",
        "expected_outputs",
        "requires_current_state",
        "requires_external_evidence",
        "requires_write",
        "execution_requested",
    )
    result: dict[str, Any] = {}
    for key in allowed:
        item = payload.get(key)
        if item not in (None, "", [], {}):
            result[key] = item
    return result


def _estimate_llm_tokens(value: Any) -> int:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value or "")
    return max(1, len(text) // 4)


def _record_runtime_llm_call(
    context: dict[str, Any],
    *,
    estimated_tokens: int,
    llm_service: LLMService | None = None,
) -> None:
    budget = context.get("runtime_budget")
    recorder = getattr(budget, "record_llm_call", None)
    if not callable(recorder):
        return
    usage = dict(llm_service.last_usage or {}) if llm_service is not None else {}
    actual = int(usage.get("total_tokens") or 0)
    recorder(token_estimate=actual or max(0, int(estimated_tokens or 0)))


def _target_design_prompt(payload: dict[str, Any]) -> list[dict[str, str]]:
    system = r"""你是模拟盘新持仓策略设计 Agent，也是本步骤的业务决策者。

你的职责是根据用户本轮原始问题、当前持仓、风险报告、用户画像和真实候选数据，生成一套完整的新目标持仓策略。

职责边界：
- 业务语义、证券选择、目标权重、现金权重、策略参数和理由由你决定。
- 历史持仓、历史策略参数和用户画像是决策上下文；除非用户本轮明确要求继承，否则不能把历史策略限制自动当成新策略硬约束。
- 只能使用 available_security_universe 中真实存在的证券标识。
- 不得依赖关键词模板解释用户目标。
- 不得输出隐藏推理，只输出 JSON。

必须满足的输出契约：
1. selected_candidates 必须是非空数组；
2. 每个证券标识唯一，且 target_weight 必须大于 0；
3. target_cash_weight 必须位于 [0, 1]；
4. 所有 target_weight 与 target_cash_weight 的合计必须等于 1，允许误差不超过 0.000001；
5. 如果输入包含 repair_context，必须根据其中的 validation_feedback 修正错误并重新输出完整方案，不能只返回差异；
6. 如果缺少无法从系统获得且确实必须由用户提供的信息，才设置 need_clarification=true；系统数据不足不能伪装成用户缺失信息；
7. 只做只读建议，不生成订单，不修改模拟盘。

输出格式：
{
  "target_design": {
    "target_cash_weight": 0.0,
    "selected_candidates": [
      {
        "stock_code": "",
        "target_weight": 0.0,
        "selection_reason": ""
      }
    ],
    "strategy_parameters": {},
    "design_rationale": [],
    "differences_from_current": [],
    "assumptions": [],
    "source_map": {},
    "missing_information": [],
    "need_clarification": false,
    "clarification_question": "",
    "confidence": 0.0
  }
}"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]


def _validate_target_design_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload.get("target_design"), dict):
        raise ValueError("llm_target_design_missing")


def _allocation_validation_feedback(
    *,
    raw_candidates: Any,
    requested_cash_weight: float | None,
    universe_map: dict[str, dict[str, Any]],
    max_single_weight: float | None = None,
    max_industry_weight: float | None = None,
    constraint_sources: dict[str, str] | None = None,
    expected_target_position_count: int | None = None,
    design_rationale: Any = None,
    tolerance: float = 1e-6,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate an LLM-authored allocation without changing business output.

    User-profile and explicit risk limits are hard validation constraints.  The
    validator reports violations with enough facts for a bounded redesign, but
    never silently clips weights or invents industry classifications.
    """

    errors: list[dict[str, Any]] = []
    canonical: list[dict[str, Any]] = []
    seen: set[str] = set()
    total_stock_weight = 0.0

    if not isinstance(raw_candidates, list) or not raw_candidates:
        errors.append(
            {
                "code": "selected_candidates_required",
                "field": "selected_candidates",
                "message": "selected_candidates 必须是非空数组。",
                "repairable_by_llm": True,
            }
        )
        raw_candidates = []

    for index, item in enumerate(raw_candidates):
        field_prefix = f"selected_candidates[{index}]"
        if not isinstance(item, dict):
            errors.append(
                {
                    "code": "candidate_must_be_object",
                    "field": field_prefix,
                    "message": "候选项必须是对象。",
                    "repairable_by_llm": True,
                }
            )
            continue

        code = _code(item.get("stock_code") or item.get("code") or item.get("symbol"))
        if not code:
            errors.append(
                {
                    "code": "security_identifier_required",
                    "field": f"{field_prefix}.stock_code",
                    "message": "证券标识不能为空。",
                    "repairable_by_llm": True,
                }
            )
            continue
        if code not in universe_map:
            errors.append(
                {
                    "code": "security_not_in_available_universe",
                    "field": f"{field_prefix}.stock_code",
                    "value": code,
                    "message": "证券不在本轮真实候选或当前持仓数据中。",
                    "repairable_by_llm": True,
                }
            )
            continue
        if code in seen:
            errors.append(
                {
                    "code": "duplicate_security",
                    "field": f"{field_prefix}.stock_code",
                    "value": code,
                    "message": "同一证券不能重复出现。",
                    "repairable_by_llm": True,
                }
            )
            continue

        raw_weight = _float(item.get("target_weight"), None)
        if raw_weight is not None and raw_weight > 1.0 and raw_weight <= 100.0:
            raw_weight /= 100.0
        if raw_weight is not None and raw_weight < 0:
            errors.append(
                {
                    "code": "negative_target_weight",
                    "field": f"{field_prefix}.target_weight",
                    "value": raw_weight,
                    "message": "target_weight must not be negative",
                    "repairable_by_llm": True,
                }
            )
            continue
        weight = raw_weight
        if weight is None:
            errors.append(
                {
                    "code": "target_weight_required",
                    "field": f"{field_prefix}.target_weight",
                    "message": "目标权重不能为空。",
                    "repairable_by_llm": True,
                }
            )
            continue
        if weight <= 0 or weight > 1:
            errors.append(
                {
                    "code": "target_weight_out_of_range",
                    "field": f"{field_prefix}.target_weight",
                    "value": weight,
                    "expected": "0 < target_weight <= 1",
                    "message": "目标权重必须大于0且不超过1。",
                    "repairable_by_llm": True,
                }
            )
            continue

        seen.add(code)
        source = universe_map[code]
        if max_single_weight is not None and float(weight) > float(max_single_weight) + tolerance:
            errors.append(
                {
                    "code": "single_position_limit_exceeded",
                    "field": f"{field_prefix}.target_weight",
                    "stock_code": code,
                    "observed_weight": round(float(weight), 10),
                    "max_allowed_weight": round(float(max_single_weight), 10),
                    "constraint_source": str((constraint_sources or {}).get("max_single_weight") or "risk_constraint"),
                    "message": "target weight exceeds the enforced single-position limit",
                    "repairable_by_llm": True,
                }
            )
        total_stock_weight += float(weight)
        canonical.append(
            {
                "stock_code": code,
                "stock_name": source.get("stock_name") or str(item.get("stock_name") or ""),
                "industry": source.get("industry") or "",
                "target_weight": float(weight),
                "selection_reason": str(item.get("selection_reason") or item.get("reason") or "").strip(),
            }
        )

    if requested_cash_weight is None:
        errors.append(
            {
                "code": "target_cash_weight_required",
                "field": "target_cash_weight",
                "message": "目标现金权重不能为空。",
                "repairable_by_llm": True,
            }
        )
    elif requested_cash_weight < 0 or requested_cash_weight > 1:
        errors.append(
            {
                "code": "target_cash_weight_out_of_range",
                "field": "target_cash_weight",
                "value": requested_cash_weight,
                "expected": "0 <= target_cash_weight <= 1",
                "message": "目标现金权重必须位于0到1之间。",
                "repairable_by_llm": True,
            }
        )

    total_weight = total_stock_weight + float(requested_cash_weight or 0.0)
    if requested_cash_weight is not None and abs(total_weight - 1.0) > tolerance:
        errors.append(
            {
                "code": "portfolio_weight_sum_mismatch",
                "field": "selected_candidates[*].target_weight + target_cash_weight",
                "observed": round(total_weight, 10),
                "expected": 1.0,
                "tolerance": tolerance,
                "message": "股票权重与现金权重合计必须等于1。",
                "repairable_by_llm": True,
            }
        )

    if expected_target_position_count is not None and len(canonical) != int(expected_target_position_count):
        errors.append(
            {
                "code": "target_position_count_mismatch",
                "field": "selected_candidates",
                "observed": len(canonical),
                "expected": int(expected_target_position_count),
                "repairable_by_llm": True,
            }
        )

    industry_metadata = _industry_metadata_status(canonical)
    if max_industry_weight is not None and canonical:
        if not industry_metadata["verifiable"]:
            errors.append(
                {
                    "code": "industry_constraint_unverifiable",
                    "field": "selected_candidates[*].industry",
                    "constraint_source": str((constraint_sources or {}).get("max_industry_weight") or "risk_constraint"),
                    "coverage": industry_metadata["coverage"],
                    "message": "industry limit cannot be verified because industry metadata is incomplete",
                    "repairable_by_llm": False,
                }
            )
        else:
            for industry, exposure in _industry_exposure(canonical, "target_weight").items():
                if exposure > float(max_industry_weight) + tolerance:
                    errors.append(
                        {
                            "code": "industry_weight_limit_exceeded",
                            "field": "selected_candidates[*].industry",
                            "industry": industry,
                            "observed_weight": exposure,
                            "max_allowed_weight": round(float(max_industry_weight), 10),
                            "constraint_source": str((constraint_sources or {}).get("max_industry_weight") or "risk_constraint"),
                            "repairable_by_llm": True,
                        }
                    )

    rationale_text = " ".join(str(item) for item in (design_rationale or []) if str(item).strip()).lower()
    if max_single_weight is not None and any(marker in rationale_text for marker in ("符合", "满足", "不超过", "complies", "satisfies")):
        allowed_percent = f"{float(max_single_weight) * 100:g}%"
        observed_max = max((float(item.get("target_weight") or 0.0) for item in canonical), default=0.0)
        if allowed_percent in rationale_text and observed_max > float(max_single_weight) + tolerance:
            errors.append(
                {
                    "code": "design_explanation_conflict",
                    "field": "design_rationale",
                    "observed_max_single_weight": round(observed_max, 10),
                    "max_allowed_weight": round(float(max_single_weight), 10),
                    "repairable_by_llm": True,
                }
            )

    return canonical, {
        "valid": not errors,
        "errors": errors,
        "repairable": bool(errors) and all(bool(item.get("repairable_by_llm")) for item in errors),
        "stock_weight_sum": round(total_stock_weight, 10),
        "cash_weight": requested_cash_weight,
        "total_weight": round(total_weight, 10),
        "tolerance": tolerance,
        "industry_metadata": industry_metadata,
        "enforced_constraints": {
            "max_single_weight": max_single_weight,
            "max_industry_weight": max_industry_weight,
            "sources": dict(constraint_sources or {}),
        },
        "mutation_performed": False,
    }


def _invalid_design_result(
    *,
    design: dict[str, Any],
    validation_feedback: dict[str, Any],
    limitations: list[str] | None = None,
    stage: str,
) -> dict[str, Any]:
    repairable = bool(validation_feedback.get("repairable"))
    return {
        "success": False,
        "status": "invalid_llm_target_design",
        "message": "LLM 生成的新策略未通过校验，结果未被构造器修改。" if repairable else "新策略未通过校验，且当前问题无法通过自动重规划修复。",
        "errors": ["invalid_llm_target_design"],
        "warnings": list(limitations or []),
        "data": {
            "target_design": design,
            "validation_feedback": validation_feedback,
            "validation_stage": stage,
            "replan_required": repairable,
            "repairable": repairable,
            "replan_scope": "target_design" if repairable else "none",
            "next_action": "replan_target_design" if repairable else "report_limitation",
            "need_clarification": False,
            "not_executed": True,
            "mutation_performed": False,
        },
    }


def design_target_portfolio_adapter(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Ask the LLM to author a new strategy, then validate without rewriting it."""

    run_id = str(context.get("run_id") or "")
    task_id = str(context.get("task_id") or "")
    current_rows = _position_rows(args.get("current_portfolio"))
    ranking_rows = _ranking_rows(args.get("ranking"))
    profile = _profile_summary(args.get("user_profile"))
    risk = _risk_summary(args.get("risk_report"))
    current_cash = _account_cash_ratio(args.get("current_portfolio"))
    current_total_assets = _account_total_assets(args.get("current_portfolio"))
    compact_user_goal = _compact_user_goal(args.get("user_goal"))
    compact_user_request = str(
        compact_user_goal.get("raw_message")
        or compact_user_goal.get("resolved_message")
        or args.get("query")
        or context.get("raw_query")
        or context.get("query")
        or ""
    ).strip()[:2400]

    if not current_rows and not ranking_rows:
        return {
            "success": False,
            "status": "missing_required_sources",
            "message": "系统没有可供新策略选择和验证的真实证券数据。",
            "errors": ["missing_required_sources"],
            "data": {
                "need_clarification": False,
                "missing_information": ["available_security_universe"],
                "replan_required": False,
                "repairable": False,
                "next_action": "report_limitation",
                "not_executed": True,
            },
        }

    reference_constraints, reference_constraint_sources = _resolve_risk_constraints(
        user_profile=args.get("user_profile"),
        risk_report=args.get("risk_report"),
    )
    current_map = {row["stock_code"]: row for row in current_rows}
    ranking_map = {row["stock_code"]: row for row in ranking_rows}
    universe_map = {**current_map, **ranking_map}
    universe = list(universe_map.values())
    current_industry_metadata = _industry_metadata_status(current_rows)
    universe_industry_metadata = _industry_metadata_status(universe)
    repair_context = _unwrap(args.get("construction_feedback"))
    if not isinstance(repair_context, dict):
        repair_context = {}

    flow_event(
        "TARGET_DESIGN_INPUT",
        {
            "user_request": compact_user_request,
            "user_goal": compact_user_goal,
            "current_position_count": len(current_rows),
            "candidate_universe_count": len(universe),
            "current_cash_weight": current_cash,
            "current_total_assets": current_total_assets,
            "reference_constraints": reference_constraints,
            "reference_constraint_sources": reference_constraint_sources,
            "reference_constraints_enforced": True,
            "decision_authority": "llm",
            "repair_round": repair_context.get("replan_round"),
            "validator_mutation_allowed": False,
        },
        run_id=run_id,
        task_id=task_id,
    )

    llm_service = _runtime_llm_service(context)
    if llm_service is None or not llm_service.is_available:
        return {
            "success": False,
            "status": "llm_unavailable",
            "message": "缺少当前 Agent Run 的 LLM 运行时依赖，无法完成本轮新策略设计。",
            "errors": ["llm_unavailable"],
            "data": {"need_clarification": False, "retryable": True, "repairable": False, "not_executed": True},
        }

    payload = {
        "user_request": compact_user_request,
        "user_goal": compact_user_goal,
        "current_portfolio": {
            "position_count": len(current_rows),
            "cash_weight": current_cash,
            "total_assets": current_total_assets,
            "positions": current_rows,
        },
        "risk_report": risk,
        "user_profile_or_previous_strategy": profile,
        "enforced_constraints": {
            **reference_constraints,
            "sources": reference_constraint_sources,
            "enforcement": "strict_validation_no_silent_clipping",
        },
        "available_security_universe": universe,
        "repair_context": repair_context,
        "validation_contract": {
            "security_must_exist_in_available_universe": True,
            "unique_security_identifiers": True,
            "positive_security_weights": True,
            "cash_weight_range": [0, 1],
            "portfolio_total": 1.0,
            "portfolio_total_tolerance": 1e-6,
            "max_single_weight": reference_constraints.get("max_single_weight"),
            "max_industry_weight": reference_constraints.get("max_industry_weight"),
            "industry_metadata_must_be_complete_when_industry_limit_exists": True,
            "validator_must_not_modify_business_output": True,
            "read_only_no_mutation": True,
        },
    }
    messages = _target_design_prompt(payload)
    estimated_input_tokens = _estimate_llm_tokens(messages)
    runtime_budget = context.get("runtime_budget")
    llm_guard = getattr(runtime_budget, "ensure_can_start_llm", None)
    if callable(llm_guard):
        try:
            llm_guard(additional_tokens=estimated_input_tokens)
        except Exception as exc:
            trace_exception("portfolio.target.design.llm_budget_blocked", exc, run_id=run_id, task_id=task_id)
            return {
                "success": False,
                "status": "llm_budget_unavailable",
                "message": "当前模型调用预算不足，未调用目标策略设计模型。",
                "errors": ["llm_budget_unavailable"],
                "data": {"need_clarification": False, "retryable": False, "repairable": False, "not_executed": True},
            }

    try:
        parsed = llm_service.generate_json(
            stage="completion",
            messages=messages,
            max_output_tokens=2400,
            operation="target_portfolio_design",
            validator=_validate_target_design_payload,
        )
        _record_runtime_llm_call(
            context,
            estimated_tokens=estimated_input_tokens + _estimate_llm_tokens(parsed),
            llm_service=llm_service,
        )
        design = dict(parsed.get("target_design") or {})
    except Exception as exc:
        _record_runtime_llm_call(context, estimated_tokens=0, llm_service=llm_service)
        trace_exception("portfolio.target.design.llm_failed", exc, run_id=run_id, task_id=task_id)
        return {
            "success": False,
            "status": "llm_target_design_failed",
            "message": f"LLM 新策略设计失败：{type(exc).__name__}",
            "errors": ["llm_target_design_failed"],
            "data": {
                "retryable": True,
                "repairable": True,
                "replan_required": True,
                "replan_scope": "target_design",
                "next_action": "replan_target_design",
                "missing_sources": ["target_design"],
                "not_executed": True,
            },
        }

    requested_cash_weight = _float(design.get("target_cash_weight"), None)
    if requested_cash_weight is not None and requested_cash_weight > 1.0 and requested_cash_weight <= 100.0:
        requested_cash_weight /= 100.0
    expected_target_count = _float(
        profile.get("target_position_count") or profile.get("preferred_position_count"),
        None,
    )
    selected_candidates, validation_feedback = _allocation_validation_feedback(
        raw_candidates=design.get("selected_candidates"),
        requested_cash_weight=requested_cash_weight,
        universe_map=universe_map,
        max_single_weight=reference_constraints.get("max_single_weight"),
        max_industry_weight=reference_constraints.get("max_industry_weight"),
        constraint_sources=reference_constraint_sources,
        expected_target_position_count=int(expected_target_count) if expected_target_count and expected_target_count > 0 else None,
        design_rationale=design.get("design_rationale"),
    )

    limitations: list[str] = []
    if not bool(universe_industry_metadata.get("verifiable")):
        limitations.append("候选证券行业元数据覆盖不足，行业相关结论暂不可完整验证。")
    raw_missing_information = [str(item) for item in (design.get("missing_information") or []) if str(item).strip()]
    classified, resolved_claims = _classify_design_limitations(raw_missing_information, total_assets=current_total_assets)
    limitations.extend(classified)
    limitations = list(dict.fromkeys(limitations))

    design["selected_candidates"] = selected_candidates if validation_feedback.get("valid") else list(design.get("selected_candidates") or [])
    design["target_cash_weight"] = requested_cash_weight
    design["target_position_count"] = len(selected_candidates) if validation_feedback.get("valid") else len(design.get("selected_candidates") or [])
    design["candidate_policy"] = "llm_selected"
    design["allocation_method"] = "llm_authored_weights"
    design["confidence"] = float(_float(design.get("confidence"), 0.0) or 0.0)
    design["strategy_parameters"] = dict(design.get("strategy_parameters") or {})
    design["design_rationale"] = [str(item) for item in (design.get("design_rationale") or []) if str(item).strip()][:16]
    design["differences_from_current"] = [str(item) for item in (design.get("differences_from_current") or []) if str(item).strip()][:16]
    design["assumptions"] = [str(item) for item in (design.get("assumptions") or []) if str(item).strip()][:16]
    design["source_map"] = dict(design.get("source_map") or {})
    design["raw_missing_information"] = raw_missing_information
    design["resolved_missing_claims"] = resolved_claims
    design["system_data_limitations"] = limitations
    design["missing_information"] = []
    design["need_clarification"] = False
    design["clarification_question"] = ""
    design["reference_strategy_constraints"] = reference_constraints
    design["reference_constraint_sources"] = reference_constraint_sources
    design["reference_constraints_enforced"] = True
    design["current_total_assets"] = current_total_assets
    design["industry_metadata"] = {
        "current_portfolio": current_industry_metadata,
        "available_security_universe": universe_industry_metadata,
    }
    design["validation_feedback"] = validation_feedback
    design["validator_mutation_performed"] = False

    flow_event(
        "TARGET_DESIGN",
        {
            "llm_target_design": design,
            "candidate_decision_source": "llm",
            "strategy_authority": "llm",
            "reference_constraints_enforced": True,
            "available_security_count": len(universe),
            "validation_feedback": validation_feedback,
            "validator_mutation_performed": False,
            "next_step": "construct target portfolio" if validation_feedback.get("valid") else "bounded LLM redesign",
        },
        run_id=run_id,
        task_id=task_id,
        level="INFO" if validation_feedback.get("valid") else "WARNING",
    )

    if not validation_feedback.get("valid"):
        return _invalid_design_result(
            design=design,
            validation_feedback=validation_feedback,
            limitations=limitations,
            stage="design_output_validation",
        )

    return {
        "success": True,
        "status": "success",
        "message": "LLM 已根据本轮用户问题生成并通过校验的新目标持仓策略。",
        "warnings": limitations,
        "data": {
            "target_design": design,
            "validation_feedback": validation_feedback,
            "system_data_limitations": limitations,
            "not_executed": True,
            "mutation_performed": False,
        },
    }


def construct_target_portfolio_adapter(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Validate and materialize an LLM design without changing its decisions."""

    run_id = str(context.get("run_id") or "")
    task_id = str(context.get("task_id") or "")
    trace_event(
        "portfolio.target.construct.start",
        {
            "argument_keys": sorted(args),
            "decision_source": "target_design.selected_candidates",
            "strategy_authority": "llm",
            "validator_mutation_allowed": False,
        },
        run_id=run_id,
        task_id=task_id,
    )

    design_payload = _unwrap(args.get("target_design"))
    design = dict(design_payload) if isinstance(design_payload, dict) else {}
    if not design:
        return {
            "success": False,
            "status": "missing_llm_target_design_or_sources",
            "message": "缺少 LLM 生成的新策略设计，需要重新规划。",
            "errors": ["missing_llm_target_design_or_sources"],
            "data": {
                "replan_required": True,
                "repairable": True,
                "replan_scope": "target_design",
                "next_action": "replan_target_design",
                "missing_sources": ["target_design"],
                "not_executed": True,
                "mutation_performed": False,
            },
        }

    current_rows = _position_rows(args.get("current_portfolio"))
    ranking_rows = _ranking_rows(args.get("ranking"))
    current_map = {row["stock_code"]: row for row in current_rows}
    ranking_map = {row["stock_code"]: row for row in ranking_rows}
    universe_map = {**current_map, **ranking_map}
    if not universe_map:
        return _system_data_missing_result(
            "当前系统没有可验证 LLM 选择结果的证券数据。",
            ["available_security_universe"],
            next_action="report_limitation",
        )

    enforced_constraints, enforced_constraint_sources = _resolve_risk_constraints(
        user_profile=args.get("user_profile"),
        risk_report=args.get("risk_report"),
        explicit_single=args.get("max_single_weight"),
        explicit_industry=args.get("max_industry_weight"),
    )
    requested_design_cash = _float(design.get("target_cash_weight"), None)
    if requested_design_cash is not None and requested_design_cash > 1.0 and requested_design_cash <= 100.0:
        requested_design_cash /= 100.0
    selected, validation_feedback = _allocation_validation_feedback(
        raw_candidates=design.get("selected_candidates"),
        requested_cash_weight=requested_design_cash,
        universe_map=universe_map,
        max_single_weight=enforced_constraints.get("max_single_weight"),
        max_industry_weight=enforced_constraints.get("max_industry_weight"),
        constraint_sources=enforced_constraint_sources,
        expected_target_position_count=_float(design.get("target_position_count"), None),
        design_rationale=design.get("design_rationale"),
    )
    if not validation_feedback.get("valid"):
        return _invalid_design_result(
            design=design,
            validation_feedback=validation_feedback,
            limitations=list(design.get("system_data_limitations") or []),
            stage="construction_precondition_validation",
        )

    cash_weight = float(design.get("target_cash_weight"))
    total_assets = _account_total_assets(args.get("current_portfolio"))
    target_rows: list[dict[str, Any]] = []
    quantity_limitations: list[str] = []
    for candidate in selected:
        code = candidate["stock_code"]
        weight = float(candidate["target_weight"])
        current = current_map.get(code, {})
        price = _float(candidate.get("current_price") or current.get("current_price"), 0.0) or 0.0
        target_amount = float(total_assets) * weight if total_assets is not None else None
        target_quantity: float | None = None
        estimated_actual_weight: float | None = None
        if target_amount is not None and target_amount >= 0 and price > 0:
            target_quantity = float(math.floor(target_amount / price / 100.0) * 100)
            estimated_actual_weight = target_quantity * price / float(total_assets) if total_assets and total_assets > 0 else None
        else:
            quantity_limitations.append(f"{code} 缺少有效价格或账户总资产，未生成目标股数。")
        current_weight = float(current.get("current_weight") or 0.0)
        target_rows.append(
            {
                "stock_code": code,
                "stock_name": candidate.get("stock_name") or current.get("stock_name") or "",
                "industry": candidate.get("industry") or current.get("industry") or "",
                "rank": candidate.get("rank"),
                "score": candidate.get("score"),
                "selection_reason": candidate.get("selection_reason") or "",
                "current_weight": round(current_weight, 8),
                "target_weight": weight,
                "weight_delta": round(weight - current_weight, 8),
                "current_price": price,
                "target_amount": round(target_amount, 2) if target_amount is not None else None,
                "target_quantity": target_quantity,
                "estimated_actual_weight": round(estimated_actual_weight, 8) if estimated_actual_weight is not None else None,
            }
        )

    selected_codes = {item["stock_code"] for item in target_rows}
    removed = [
        {
            "stock_code": row["stock_code"],
            "stock_name": row["stock_name"],
            "current_weight": row["current_weight"],
            "target_weight": 0.0,
            "weight_delta": round(-row["current_weight"], 8),
        }
        for row in current_rows
        if row["stock_code"] not in selected_codes
    ]

    current_cash = _account_cash_ratio(args.get("current_portfolio"))
    current_snapshot = _risk_snapshot(current_rows, "current_weight", current_cash)
    target_snapshot = _risk_snapshot(target_rows, "target_weight", cash_weight)
    comparable_metrics = ["max_single_weight", "concentration_hhi"]
    if current_snapshot.get("industry_constraint_verifiable") and target_snapshot.get("industry_constraint_verifiable"):
        comparable_metrics.append("max_industry_weight")
    improved_metrics = [
        metric for metric in comparable_metrics
        if float(target_snapshot.get(metric) or 0.0) < float(current_snapshot.get(metric) or 0.0) - 1e-10
    ]
    worsened_metrics = [
        metric for metric in comparable_metrics
        if float(target_snapshot.get(metric) or 0.0) > float(current_snapshot.get(metric) or 0.0) + 1e-10
    ]

    reference_constraints, reference_constraint_sources = enforced_constraints, enforced_constraint_sources
    reference_comparison: dict[str, Any] = {}
    ref_single = reference_constraints.get("max_single_weight")
    ref_industry = reference_constraints.get("max_industry_weight")
    if ref_single is not None:
        reference_comparison["max_single_weight"] = {
            "reference_value": round(float(ref_single), 8),
            "target_value": round(float(target_snapshot.get("max_single_weight") or 0.0), 8),
            "satisfied": float(target_snapshot.get("max_single_weight") or 0.0) <= float(ref_single) + 1e-10,
            "status": "enforced",
        }
    if ref_industry is not None:
        reference_comparison["max_industry_weight"] = {
            "reference_value": round(float(ref_industry), 8),
            "target_value": target_snapshot.get("max_industry_weight"),
            "satisfied": None if not target_snapshot.get("industry_constraint_verifiable") else float(target_snapshot.get("max_industry_weight") or 0.0) <= float(ref_industry) + 1e-10,
            "status": "enforced" if target_snapshot.get("industry_constraint_verifiable") else "unverifiable",
        }

    limitations = ["目标组合仅为只读分析结果，没有生成订单或修改模拟盘。", *quantity_limitations]
    if not target_snapshot.get("industry_constraint_verifiable"):
        limitations.append("行业元数据不完整，行业相关指标暂不可完整验证。")
    limitations.extend(list(design.get("system_data_limitations") or []))
    limitations = list(dict.fromkeys(limitations))

    payload = {
        "target_positions": target_rows,
        "target_cash_weight": cash_weight,
        "target_position_count": len(target_rows),
        "candidate_policy": "llm_selected",
        "allocation_method": "llm_authored_weights",
        "candidate_decision_source": "llm",
        "strategy_authority": "llm",
        "llm_target_design": design,
        "strategy_parameters": dict(design.get("strategy_parameters") or {}),
        "design_rationale": list(design.get("design_rationale") or []),
        "differences_from_current": list(design.get("differences_from_current") or []),
        "design_assumptions": list(design.get("assumptions") or []),
        "validation_feedback": validation_feedback,
        "validator_mutation_performed": False,
        "technical_invariants_applied": {
            "valid_security_source": "validated",
            "unique_security_identifiers": "validated",
            "positive_weights": "validated",
            "total_weight_accounting": "validated_without_mutation",
            "read_only_no_mutation": "enforced",
        },
        "previous_strategy_constraints": {
            "values": reference_constraints,
            "sources": reference_constraint_sources,
            "status": "enforced",
        },
        "reference_constraint_comparison": reference_comparison,
        "current_total_assets": total_assets,
        "quantity_generation": {
            "lot_size": 100,
            "status": "complete" if not quantity_limitations else "partial",
            "limitations": list(dict.fromkeys(quantity_limitations)),
        },
        "industry_metadata_status": _industry_metadata_status(target_rows),
        "current_risk_snapshot": current_snapshot,
        "target_risk_snapshot": target_snapshot,
        "improved_risk_metrics": improved_metrics,
        "worsened_risk_metrics": worsened_metrics,
        "removed_positions": removed,
        "not_executed": True,
        "limitations": limitations,
    }

    user_id = str(args.get("user_id") or context.get("user_id") or "default")
    conversation_id = str(context.get("conversation_id") or context.get("session_id") or "")
    try:
        artifact_ref = TargetPortfolioStore(context.get("output_dir") or "outputs").save(
            payload,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            task_id=task_id,
        )
    except Exception as exc:
        trace_exception("portfolio.target.artifact_save_failed", exc, run_id=run_id, task_id=task_id)
        return {
            "success": False,
            "status": "artifact_save_failed",
            "message": "目标组合已经计算，但无法保存为可供后续对比的结构化对象。",
            "errors": ["artifact_save_failed"],
            "data": {
                "target_portfolio": payload,
                "repairable": False,
                "replan_required": False,
                "next_action": "report_limitation",
                "not_executed": True,
            },
        }

    data = {
        "target_portfolio": payload,
        "target_portfolio_ref": artifact_ref,
        "artifact_id": artifact_ref["artifact_id"],
        **payload,
    }
    flow_event(
        "TARGET_CONSTRUCTION",
        {
            "status": "success",
            "candidate_decision_source": "llm",
            "strategy_authority": "llm",
            "validator_mutation_performed": False,
            "target_position_count": len(target_rows),
            "target_cash_weight": cash_weight,
            "target_positions": target_rows,
            "current_risk_snapshot": current_snapshot,
            "target_risk_snapshot": target_snapshot,
            "target_portfolio_ref": artifact_ref,
            "not_executed": True,
        },
        run_id=run_id,
        task_id=task_id,
    )
    return {
        "success": True,
        "status": "success",
        "message": "已按 LLM 原始设计生成结构化目标组合；构造器未修改证券或目标权重。",
        "data": data,
    }


def load_target_portfolio_adapter(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_id = str(context.get("run_id") or "")
    task_id = str(context.get("task_id") or "")
    user_id = str(args.get("user_id") or context.get("user_id") or "default")
    conversation_id = str(args.get("conversation_id") or context.get("conversation_id") or context.get("session_id") or "")
    artifact_id = str(args.get("artifact_id") or "").strip()
    record, refs = TargetPortfolioStore(context.get("output_dir") or "outputs").load(
        user_id=user_id,
        conversation_id=conversation_id,
        artifact_id=artifact_id,
    )
    trace_event(
        "portfolio.target.load",
        {"requested_artifact_id": artifact_id, "available_refs": refs, "found": bool(record)},
        run_id=run_id,
        task_id=task_id,
    )
    if record is None:
        if not refs:
            question = "当前会话中没有可比较的结构化目标组合。请先生成目标组合，或提供目标组合数据。"
            reason = "target_portfolio_not_found"
        elif not artifact_id and len(refs) > 1:
            question = "当前会话中存在多个目标组合，请明确选择要比较的目标组合。"
            reason = "target_portfolio_reference_ambiguous"
        else:
            question = "没有找到指定的目标组合，请检查目标组合引用。"
            reason = "target_portfolio_not_found"
        return {
            "success": False,
            "status": reason,
            "message": question,
            "errors": [reason],
            "data": {
                "need_clarification": True,
                "clarification_question": question,
                "available_target_portfolio_refs": refs,
            },
        }
    return {
        "success": True,
        "status": "success",
        "message": "已读取结构化目标组合。",
        "data": {
            "target_portfolio": dict(record.get("payload") or {}),
            "target_portfolio_ref": {
                "artifact_id": str(record.get("artifact_id") or ""),
                "artifact_type": "target_portfolio",
                "created_at": str(record.get("created_at") or ""),
                "run_id": str(record.get("run_id") or ""),
                "task_id": str(record.get("task_id") or ""),
            },
        },
    }


def compare_portfolios_adapter(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    run_id = str(context.get("run_id") or "")
    task_id = str(context.get("task_id") or "")
    current_value = args.get("current_portfolio")
    target_value = args.get("target_portfolio")
    if current_value in (None, "") or target_value in (None, ""):
        missing = [name for name, value in (("current_portfolio", current_value), ("target_portfolio", target_value)) if value in (None, "")]
        return _missing_result(
            "缺少比较所需的组合对象，本次没有只返回其中一个组合。",
            missing,
            question="请提供当前组合和目标组合，或先明确要读取的目标组合引用。",
        )
    current_rows = _position_rows(current_value)
    target_payload = _unwrap(target_value)
    target_rows_raw = _list_first(target_payload, ("target_positions", "positions", "holdings"))
    target_rows: list[dict[str, Any]] = []
    for raw in target_rows_raw:
        if not isinstance(raw, dict):
            continue
        code = _code(raw.get("stock_code") or raw.get("code") or raw.get("ts_code"))
        if not code:
            continue
        target_rows.append(
            {
                "stock_code": code,
                "stock_name": str(raw.get("stock_name") or raw.get("name") or "").strip(),
                "industry": str(raw.get("industry") or raw.get("industry_name") or "").strip(),
                "target_weight": _ratio(raw.get("target_weight") if raw.get("target_weight") is not None else raw.get("weight"), 0.0) or 0.0,
                "target_quantity": _float(raw.get("target_quantity") or raw.get("quantity"), None),
            }
        )
    if not current_rows or not target_rows:
        return _missing_result(
            "当前组合或目标组合没有有效的持仓明细。",
            ["current_positions" if not current_rows else "target_positions"],
            question="请确认两个组合都包含股票代码和仓位信息。",
        )
    current_map = {row["stock_code"]: row for row in current_rows}
    target_map = {row["stock_code"]: row for row in target_rows}
    all_codes = sorted(set(current_map) | set(target_map))
    rows: list[dict[str, Any]] = []
    for code in all_codes:
        current = current_map.get(code, {})
        target = target_map.get(code, {})
        current_weight = float(current.get("current_weight") or 0.0)
        target_weight = float(target.get("target_weight") or 0.0)
        if current_weight <= 0 and target_weight > 0:
            change_type = "add"
        elif current_weight > 0 and target_weight <= 0:
            change_type = "remove"
        elif target_weight > current_weight + 1e-10:
            change_type = "increase"
        elif target_weight < current_weight - 1e-10:
            change_type = "decrease"
        else:
            change_type = "unchanged"
        current_quantity = _float(current.get("quantity"), 0.0) or 0.0
        target_quantity = target.get("target_quantity")
        quantity_delta = None if target_quantity is None else float(target_quantity) - current_quantity
        rows.append(
            {
                "stock_code": code,
                "stock_name": target.get("stock_name") or current.get("stock_name") or "",
                "industry": target.get("industry") or current.get("industry") or "",
                "change_type": change_type,
                "current_weight": round(current_weight, 8),
                "target_weight": round(target_weight, 8),
                "weight_delta": round(target_weight - current_weight, 8),
                "current_quantity": current_quantity,
                "target_quantity": target_quantity,
                "quantity_delta": quantity_delta,
            }
        )
    current_cash = _account_cash_ratio(current_value)
    target_cash = _ratio(_find_first(target_payload, ("target_cash_weight", "cash_weight", "cash_ratio")), 0.0) or 0.0
    current_risk = _risk_snapshot(current_rows, "current_weight", current_cash)
    target_risk = _risk_snapshot(target_rows, "target_weight", target_cash)
    comparison = {
        "rows": sorted(rows, key=lambda item: (-abs(item["weight_delta"]), item["stock_code"])),
        "added_stocks": [row for row in rows if row["change_type"] == "add"],
        "removed_stocks": [row for row in rows if row["change_type"] == "remove"],
        "increased_stocks": [row for row in rows if row["change_type"] == "increase"],
        "decreased_stocks": [row for row in rows if row["change_type"] == "decrease"],
        "unchanged_stocks": [row for row in rows if row["change_type"] == "unchanged"],
        "cash_difference": {
            "current_cash_weight": round(current_cash, 8),
            "target_cash_weight": round(target_cash, 8),
            "cash_weight_delta": round(target_cash - current_cash, 8),
        },
        "risk_before_after": {
            "current": current_risk,
            "target": target_risk,
            "delta": {
                "max_single_weight": round(
                    target_risk["max_single_weight"]
                    - current_risk["max_single_weight"],
                    8,
                ),
                "concentration_hhi": round(
                    target_risk["concentration_hhi"]
                    - current_risk["concentration_hhi"],
                    8,
                ),
                "max_industry_weight": (
                    round(
                        float(target_risk["max_industry_weight"])
                        - float(current_risk["max_industry_weight"]),
                        8,
                    )
                    if (
                        target_risk.get(
                            "industry_constraint_verifiable"
                        )
                        and current_risk.get(
                            "industry_constraint_verifiable"
                        )
                    )
                    else None
                ),
                "industry_comparison_status": (
                    "available"
                    if (
                        target_risk.get(
                            "industry_constraint_verifiable"
                        )
                        and current_risk.get(
                            "industry_constraint_verifiable"
                        )
                    )
                    else "not_verifiable"
                ),
                "cash_weight": round(
                    target_cash - current_cash,
                    8,
                ),
            },
        },
        "not_executed": True,
        "limitations": [
            "该比较只比较结构化组合数据，不生成订单、不修改模拟盘。",
            "目标组合未提供目标股数时，数量差异显示为空，只比较仓位权重。",
        ],
    }
    trace_event(
        "portfolio.compare.complete",
        {
            "row_count": len(rows),
            "added": len(comparison["added_stocks"]),
            "removed": len(comparison["removed_stocks"]),
            "increased": len(comparison["increased_stocks"]),
            "decreased": len(comparison["decreased_stocks"]),
            "risk_delta": comparison["risk_before_after"]["delta"],
        },
        run_id=run_id,
        task_id=task_id,
    )
    return {
        "success": True,
        "status": "success",
        "message": "已完成当前组合与目标组合的结构化对比；没有执行任何调仓。",
        "data": {
            "portfolio_comparison": comparison,
            "current_vs_target": comparison["rows"],
            "risk_before_after": comparison["risk_before_after"],
            "not_executed": True,
        },
    }
