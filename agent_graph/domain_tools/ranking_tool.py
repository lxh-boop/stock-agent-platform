from __future__ import annotations

from pathlib import Path
from typing import Any

def query_ranking(
    stock_code: str | None = None,
    top_k: int | str | None = 50,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    # Compatibility wrapper. Agent default path is market.get_ranking via ToolExecutor.
    # planned_removal_phase=post_phase11_1_legacy_cleanup
    from agent_graph.services.market_analysis_service import market_analysis_service

    return market_analysis_service.get_ranking(
        stock_code=stock_code,
        top_k=top_k,
        output_dir=output_dir,
        db_path=db_path,
    )
