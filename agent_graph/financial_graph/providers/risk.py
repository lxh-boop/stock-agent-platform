"""Risk-domain bridge from the graph runtime to portfolio risk services.

The adapter normalizes the existing read-only risk response and preserves the
upstream portfolio GraphRef in the result. It performs no proposal or portfolio
write operation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.financial_graph.contracts import GraphRef

from .common import records_from_payload, sources_from_payload


class PortfolioRiskProvider:
    """Risk-domain provider operations behind the GraphRef boundary."""

    def analyze_risk(
        self,
        *,
        user_id: str,
        output_dir: str | Path,
        db_path: str | Path | None,
        portfolio_ref: GraphRef | None = None,
    ) -> dict[str, Any]:
        from agent_graph.services.portfolio_risk_service import PortfolioRiskService

        raw = PortfolioRiskService().analyze_current_risk(
            user_id=user_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        return {
            "success": bool(raw.get("success")),
            "portfolio_ref": portfolio_ref.to_dict() if portfolio_ref else None,
            "message": str(raw.get("message") or ""),
            "data": raw.get("data") if isinstance(raw.get("data"), dict) else {},
            "records": records_from_payload(raw),
            "sources": sources_from_payload(raw),
            "warnings": list(raw.get("warnings") or []),
            "errors": list(raw.get("errors") or []),
        }
