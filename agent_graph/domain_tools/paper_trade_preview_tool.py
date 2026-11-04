from __future__ import annotations

from pathlib import Path

from agent_graph.domain_tools.rebalance_plan_tool import preview_add_stock_to_paper


def preview_paper_trade(
    user_id: str,
    stock_code: str,
    requested_weight: float | None = None,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    top_k: int = 50,
    session_id: str = "",
):
    return preview_add_stock_to_paper(
        user_id=user_id,
        stock_code=stock_code,
        requested_weight=requested_weight,
        output_dir=output_dir,
        db_path=db_path,
        top_k=top_k,
        session_id=session_id,
    )
