from __future__ import annotations

from pathlib import Path
from typing import Any

from portfolio.user_profile import load_user_context


def query_user_profile(
    user_id: str,
    db_path: str | Path | None = None,
    output_dir: str | Path = "outputs",
) -> dict[str, Any]:
    profile, risk, goal, constraints = load_user_context(
        user_id,
        db_path=db_path,
        output_dir=output_dir,
    )
    return {
        "user_id": str(user_id or "default"),
        "profile": profile.to_dict(),
        "risk_assessment": risk.to_dict(),
        "investment_goal": goal.to_dict(),
        "constraints": dict(constraints),
        "trading_permissions": dict(
            constraints.get("trading_permissions")
            or {}
        ),
        "status": "success",
    }
