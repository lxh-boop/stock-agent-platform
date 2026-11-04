from __future__ import annotations

import csv
from pathlib import Path

from agent_graph.services.market_analysis_service import market_analysis_service


def ranking_result(tmp_path: Path, requested: int, available: int = 60) -> dict:
    path = tmp_path / "ranking_latest.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["code", "stock_name", "pred_score", "trade_date"])
        writer.writeheader()
        for index in range(available):
            writer.writerow({"code": f"{index + 1:06d}", "stock_name": f"Stock {index + 1}", "pred_score": 1 - index / 1000, "trade_date": "2026-07-17"})
    return market_analysis_service.get_ranking(top_k=requested, output_dir=tmp_path)
