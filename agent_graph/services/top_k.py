from __future__ import annotations

from math import ceil
from typing import Any


DEFAULT_TOOL_TOP_K = 10
DEFAULT_SYSTEM_TOP_K = 10
DEFAULT_TARGET_POSITION_COUNT = 10
DEFAULT_CANDIDATE_REDUNDANCY_FACTOR = 2.0
MAX_TOP_K = 100


def _valid_top_k(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        parsed = int(str(value).strip()) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 1 <= parsed <= MAX_TOP_K else None


def resolve_requested_top_k(
    *,
    user_explicit_top_k: Any = None,
    task_top_k: Any = None,
    request_default_top_k: Any = None,
    tool_default_top_k: Any = DEFAULT_TOOL_TOP_K,
    system_fallback_top_k: Any = DEFAULT_SYSTEM_TOP_K,
) -> int:
    """Resolve an exact positive TopK using the Agent-wide precedence rule."""

    for value in (
        user_explicit_top_k,
        task_top_k,
        request_default_top_k,
        tool_default_top_k,
        system_fallback_top_k,
    ):
        resolved = _valid_top_k(value)
        if resolved is not None:
            return resolved
    return DEFAULT_SYSTEM_TOP_K


def resolve_business_top_k(
    *,
    user_explicit_top_k: Any = None,
    task_top_k: Any = None,
    target_position_count: Any = None,
    candidate_redundancy_factor: Any = DEFAULT_CANDIDATE_REDUNDANCY_FACTOR,
    request_default_top_k: Any = None,
    tool_default_top_k: Any = DEFAULT_TOOL_TOP_K,
    system_fallback_top_k: Any = DEFAULT_SYSTEM_TOP_K,
) -> int:
    """Choose ranking scope for a portfolio-design request.

    A user or planner can explicitly ask for a wider read.  Otherwise the
    ranking scope is derived from the desired number of holdings and a small,
    auditable candidate redundancy factor, rather than silently reading 50.
    """

    for value in (user_explicit_top_k, task_top_k):
        resolved = _valid_top_k(value)
        if resolved is not None:
            return resolved
    positions = _valid_top_k(target_position_count)
    if positions is not None:
        try:
            factor = float(candidate_redundancy_factor)
        except (TypeError, ValueError):
            factor = DEFAULT_CANDIDATE_REDUNDANCY_FACTOR
        factor = max(1.0, factor)
        return min(MAX_TOP_K, max(positions, int(ceil(positions * factor))))
    return resolve_requested_top_k(
        request_default_top_k=request_default_top_k,
        tool_default_top_k=tool_default_top_k,
        system_fallback_top_k=system_fallback_top_k,
    )
