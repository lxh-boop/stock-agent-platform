from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from agent_graph.services.market_analysis_service import RankingRepository
from application.paper_profile_service import save_classic_user_context
from database.repositories import PredictionRepository, RuntimeDataImportAuditRepository
from database.runtime_data_import import import_ranking_file
from portfolio.paper_account import load_paper_trading_snapshot
from portfolio.storage import PortfolioStorage


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _ranking_rows() -> list[dict]:
    return [
        {
            "trade_date": "2026-08-21",
            "prediction_date": "2026-08-24",
            "stock_code": "000001",
            "stock_name": "平安银行",
            "model_name": "Kronos-mini",
            "rank": 1,
            "score": 0.7,
        }
    ]


def test_live_ranking_does_not_fall_back_to_csv(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite"
    output_dir = tmp_path / "outputs"
    ranking_path = output_dir / "ranking_latest.csv"
    _write_csv(ranking_path, _ranking_rows())

    repository = RankingRepository(db_path)
    assert repository.load_latest_ranking(output_dir) == []
    with pytest.raises(RuntimeError, match="runtime_csv_source_disabled:ranking"):
        repository.load_latest_ranking(output_dir, ranking_path=ranking_path)


def test_portfolio_database_empty_is_authoritative(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite"
    root = tmp_path / "outputs" / "portfolio" / "alice"
    root.mkdir(parents=True)
    (root / "paper_account_latest.json").write_text(
        json.dumps({"account_id": "paper_alice", "user_id": "alice", "cash": 999}),
        encoding="utf-8",
    )
    _write_csv(
        root / "paper_positions_latest.csv",
        [{"position_id": "old", "user_id": "alice", "stock_code": "000001", "quantity": 10}],
    )

    storage = PortfolioStorage(db_path, output_dir=root, use_database=True)
    assert storage.load_account("paper_alice") is None
    assert storage.load_positions("alice") == []
    snapshot = load_paper_trading_snapshot(
        "alice",
        output_dir=tmp_path / "outputs",
        db_path=db_path,
    )
    assert snapshot["is_available"] is False
    assert snapshot["account"] == {}
    assert snapshot["positions"].empty


def test_ranking_import_is_count_validated_and_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "runtime.sqlite"
    ranking_path = tmp_path / "ranking_latest.csv"
    _write_csv(ranking_path, _ranking_rows())

    first = import_ranking_file(ranking_path, db_path=db_path)
    second = import_ranking_file(ranking_path, db_path=db_path)

    assert first["status"] == "imported"
    assert first["source_rows"] == first["imported_rows"] == 1
    assert second["status"] == "already_imported"
    assert len(PredictionRepository(db_path).list_latest_predictions()) == 1
    audit = RuntimeDataImportAuditRepository(db_path).find(
        "ranking_latest",
        first["sha256"],
    )
    assert audit is not None
    assert audit["validation_status"] == "validated"


def test_profile_database_failure_is_not_reported_as_file_fallback(tmp_path: Path) -> None:
    class BrokenRepository:
        def __init__(self, _db_path=None):
            pass

        def insert_user_profile(self, _record):
            raise RuntimeError("database unavailable")

    output_dir = tmp_path / "outputs"
    with pytest.raises(RuntimeError, match="database unavailable"):
        save_classic_user_context(
            {"user_id": "alice", "initial_capital": 100000},
            output_dir=output_dir,
            repository_factory=BrokenRepository,
        )
    assert not (output_dir / "users" / "alice" / "user_profile.json").exists()
