from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.services.portfolio_service import portfolio_service
from agent_graph.domain_tools._common import portfolio_user_dir, safe_float
from portfolio.portfolio_risk import calculate_portfolio_risk
from portfolio.storage import PortfolioStorage
from portfolio.user_profile import build_user_constraints, default_user_profile, load_user_context


class RiskRepository:
    def storage(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> PortfolioStorage:
        return PortfolioStorage(db_path, output_dir=portfolio_user_dir(output_dir, user_id))

    def load_stored_risk_report(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any] | None:
        return self.storage(user_id, output_dir=output_dir, db_path=db_path).load_risk_report()

    def load_risk_inputs(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> tuple[Any, list[Any]]:
        storage = self.storage(user_id, output_dir=output_dir, db_path=db_path)
        return storage.load_account(f"paper_{user_id}"), storage.load_positions(user_id)


class PortfolioRiskService:
    def __init__(self, *, risk_repository: RiskRepository | None = None) -> None:
        self.risk_repository = risk_repository or RiskRepository()

    def _constraints(self, user_id: str, db_path: str | Path | None = None) -> dict[str, Any]:
        try:
            _, _, _, constraints = load_user_context(user_id, db_path=db_path)
            return dict(constraints or {})
        except Exception:
            return build_user_constraints(default_user_profile(user_id))

    def calculate_concentration(self, positions: list[dict[str, Any]], total_assets: float) -> dict[str, Any]:
        total = safe_float(total_assets, 0.0)
        industry_exposure: dict[str, float] = {}
        single_weights: dict[str, float] = {}
        for row in positions or []:
            if safe_float(row.get("quantity"), 0.0) <= 0:
                continue
            code = str(row.get("stock_code") or "")
            weight = safe_float(row.get("position_ratio"), 0.0)
            if weight <= 0 and total > 0:
                weight = safe_float(row.get("market_value"), 0.0) / total
            if code:
                single_weights[code] = weight
            industry = str(row.get("industry") or "unknown")
            industry_exposure[industry] = industry_exposure.get(industry, 0.0) + weight
        return {
            "single_stock_weights": single_weights,
            "max_single_position": max(single_weights.values(), default=0.0),
            "industry_exposure": industry_exposure,
            "max_industry_exposure": max(industry_exposure.values(), default=0.0),
        }

    def calculate_single_stock_weight(
        self,
        stock_code: str,
        *,
        user_id: str,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        state = portfolio_service.get_portfolio_state(user_id, output_dir=output_dir, db_path=db_path)
        code = str(stock_code or "").split(".")[0].zfill(6)
        weight = safe_float((state.get("position_weights") or {}).get(code), 0.0)
        return {"stock_code": code, "weight": weight, "not_executed": True, "mutation_performed": False}

    def calculate_industry_exposure(
        self,
        *,
        user_id: str,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        state = portfolio_service.get_portfolio_state(user_id, output_dir=output_dir, db_path=db_path)
        concentration = self.calculate_concentration(
            list(state.get("positions") or []),
            safe_float((state.get("account_summary") or {}).get("total_assets"), 0.0),
        )
        return {
            "industry_exposure": concentration["industry_exposure"],
            "max_industry_exposure": concentration["max_industry_exposure"],
            "not_executed": True,
            "mutation_performed": False,
        }

    def calculate_drawdown(self, account: dict[str, Any] | None) -> float:
        account = dict(account or {})
        return abs(min(0.0, safe_float(account.get("max_drawdown") or account.get("drawdown"), 0.0)))

    def build_risk_summary(self, risk_report: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
        state = dict(state or {})
        return {
            "risk_level": risk_report.get("risk_level", ""),
            "holding_count": risk_report.get("holding_count", state.get("position_count", 0)),
            "risk_warning_count": len(risk_report.get("risk_warnings") or []),
            "max_single_position": safe_float(risk_report.get("max_single_position"), 0.0),
            "cash_ratio": safe_float(risk_report.get("cash_ratio"), 0.0),
            "max_drawdown": safe_float(risk_report.get("max_drawdown"), 0.0),
            "user_risk_match": bool(risk_report.get("user_risk_match", True)),
        }

    def analyze_current_risk(
        self,
        user_id: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        portfolio_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user = str(user_id or "default")
        state = dict(portfolio_state or portfolio_service.get_portfolio_state(user, output_dir=output_dir, db_path=db_path))
        consistency_status = str(state.get("consistency_status") or "")
        if consistency_status in {"missing_account", "rejected"}:
            status = "missing_account" if consistency_status == "missing_account" else "invalid_portfolio_snapshot"
            return {
                "status": status,
                "risk_report": {},
                "risk": {},
                "source": "normalized_snapshot_rejected",
                "account": state.get("account") or {},
                "positions": state.get("positions") or [],
                "summary": {"risk_level": "", "holding_count": state.get("position_count", 0)},
                "as_of_date": state.get("as_of_date", ""),
                "consistency_status": consistency_status,
                "consistency_errors": list(state.get("consistency_errors") or []),
                "snapshot_id": str(state.get("snapshot_id") or ""),
                "calculation_trace": dict(state.get("calculation_trace") or {}),
                "error_code": str(state.get("error_code") or "portfolio_snapshot_inconsistent"),
                "safe_to_continue": False,
                "safe_to_answer": False,
                "safe_to_write": False,
                "not_executed": True,
                "mutation_performed": False,
            }

        report = calculate_portfolio_risk(
            user,
            state.get("account") or {},
            list(state.get("positions") or []),
            self._constraints(user, db_path),
        )
        risk_report = report.to_dict()
        source = "normalized_snapshot"
        status = "success"

        concentration = self.calculate_concentration(
            list(state.get("positions") or []),
            safe_float((state.get("account_summary") or {}).get("total_assets"), 0.0),
        )
        risk_report.setdefault("industry_concentration", concentration["industry_exposure"])
        summary = self.build_risk_summary(risk_report, state)
        return {
            "status": status,
            "risk_report": risk_report,
            "risk": risk_report,
            "current_risk": risk_report,
            "risk_factors": list(risk_report.get("risk_warnings") or []),
            "limitations": [],
            "concentration": concentration,
            "account": state.get("account") or {},
            "positions": state.get("positions") or [],
            "summary": summary,
            "source": source,
            "as_of_date": state.get("as_of_date", ""),
            "consistency_status": consistency_status,
            "consistency_warnings": list(state.get("consistency_warnings") or []),
            "snapshot_id": str(state.get("snapshot_id") or ""),
            "cash_semantics": str(state.get("cash_semantics") or "uninvested_cash"),
            "calculation_trace": dict(state.get("calculation_trace") or {}),
            "safe_to_continue": bool(state.get("safe_to_continue", True)),
            "safe_to_answer": bool(state.get("safe_to_answer", True)),
            "safe_to_write": bool(state.get("safe_to_write", True)),
            "not_executed": True,
            "mutation_performed": False,
        }

    def compare_risk_before_after(
        self,
        user_id: str,
        *,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        current = self.analyze_current_risk(user_id, output_dir=output_dir, db_path=db_path)
        before_report = dict(before or current.get("risk_report") or {})
        after_report = dict(after or current.get("risk_report") or {})
        keys = ["max_single_position", "cash_ratio", "max_drawdown", "high_risk_position_ratio"]
        delta = {
            key: safe_float(after_report.get(key), 0.0) - safe_float(before_report.get(key), 0.0)
            for key in keys
        }
        return {
            "status": "success" if before_report or after_report else current.get("status", "missing_account"),
            "before": before_report,
            "after": after_report,
            "delta": delta,
            "summary": {
                "risk_level_before": before_report.get("risk_level", ""),
                "risk_level_after": after_report.get("risk_level", ""),
                "warnings_before": len(before_report.get("risk_warnings") or []),
                "warnings_after": len(after_report.get("risk_warnings") or []),
            },
            "as_of_date": current.get("as_of_date", ""),
            "not_executed": True,
            "mutation_performed": False,
        }


portfolio_risk_service = PortfolioRiskService()
