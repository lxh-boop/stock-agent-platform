from __future__ import annotations

from pathlib import Path
from typing import Any


def query_portfolio_risk(
    user_id: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    # Compatibility wrapper. Agent default path is portfolio.analyze_risk via ToolExecutor.
    # planned_removal_phase=post_phase11_1_legacy_cleanup
    from agent_graph.services.portfolio_risk_service import portfolio_risk_service

    return portfolio_risk_service.analyze_current_risk(
        str(user_id or "default"),
        output_dir=output_dir,
        db_path=db_path,
    )
