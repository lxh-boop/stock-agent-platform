from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from config import DEFAULT_INITIAL_CASH, OUTPUT_DIR

class WebPaperTradingApplicationService:
    """Browser-facing application facade for Stage 6.3.

    The facade delegates every business decision and write to the existing paper-
    trading services, Tool Engine and WriteGateway. It does not calculate target
    weights, mutate portfolio state locally, or expose confirmation tokens.
    """

    def __init__(
        self,
        *,
        output_dir: str | Path = OUTPUT_DIR,
        db_path: str | Path | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.db_path = Path(db_path) if db_path else None

    @staticmethod
    def _user_id(value: Any) -> str:
        return str(value or "refactor_test").strip() or "refactor_test"

    @staticmethod
    def _result_payload(result: Any) -> dict[str, Any]:
        if hasattr(result, "to_dict") and callable(result.to_dict):
            value = result.to_dict()
            return dict(value or {}) if isinstance(value, dict) else {"result": value}
        if isinstance(result, dict):
            return dict(result)
        return {"result": result}

    def _tool_context(self, user_id: str, *, request_id: str = "") -> dict[str, Any]:
        conversation_id = f"react-paper-trading:{user_id}"
        return {
            "user_id": user_id,
            "session_id": conversation_id,
            "conversation_id": conversation_id,
            "request_id": str(request_id or ""),
            "output_dir": self.output_dir,
            "db_path": self.db_path,
        }

    @classmethod
    def _strip_write_secrets(cls, value: Any) -> Any:
        """Remove server-only proposal material before a result reaches a presenter."""

        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for raw_key, item in value.items():
                item_key = str(raw_key)
                if item_key.lower() in {"confirmation_token", "plan_hash"}:
                    continue
                output[item_key] = cls._strip_write_secrets(item)
            return output
        if isinstance(value, (list, tuple, set)):
            return [cls._strip_write_secrets(item) for item in value]
        return value

    @staticmethod
    def _confirmation_phrase(plan_id: str) -> str:
        suffix = str(plan_id or "")[-6:].upper()
        return f"CONFIRM-{suffix}" if suffix else "CONFIRM"

    def _proposal_summary(self, plan: dict[str, Any]) -> dict[str, Any]:
        plan_id = str(plan.get("plan_id") or "")
        return {
            "plan_id": plan_id,
            "intent": str(plan.get("intent") or ""),
            "operation_type": str(plan.get("operation_type") or ""),
            "confirmation_status": str(plan.get("confirmation_status") or ""),
            "execution_status": str(plan.get("execution_status") or ""),
            "expires_at": plan.get("expires_at"),
            "created_at": plan.get("created_at"),
            "before_state_summary": plan.get("before_state_summary") or {},
            "proposed_changes": plan.get("proposed_changes") or [],
            "after_state_preview": plan.get("after_state_preview") or {},
            "warnings": plan.get("warnings") or [],
            "validation_results": plan.get("validation_results") or {},
            "confirmation_phrase": self._confirmation_phrase(plan_id),
            "token_present": bool(plan.get("confirmation_token")),
        }

    def snapshot(self, user_id: str) -> dict[str, Any]:
        user_id = self._user_id(user_id)
        from portfolio.paper_account import load_paper_cash_flows, load_paper_trading_snapshot
        from pipelines.paper_backfill_pipeline import load_paper_backfill_status
        from application.paper_profile_service import (
            get_classic_user_profile_form_options,
            has_required_paper_trading_profile,
            load_classic_user_context,
            load_current_ai_reliability_state,
            load_scheduler_status_summary,
        )

        snapshot = dict(
            load_paper_trading_snapshot(
                user_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
            )
            or {}
        )
        profile = dict(
            load_classic_user_context(
                user_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
            )
            or {}
        )
        scheduler = load_scheduler_status_summary(self.output_dir) or {}
        from database.repositories import PredictionRepository
        from ranking_runtime.settings import (
            ACTIVE_MODEL_BACKEND,
            ACTIVE_MODEL_NAME,
            ACTIVE_MODEL_VERSION,
        )

        ranking_rows = PredictionRepository(self.db_path).list_latest_predictions(
            model_name=ACTIVE_MODEL_NAME,
            limit=1,
        )
        latest_ranking = dict(ranking_rows[0]) if ranking_rows else {}
        ranking_signal_date = str(
            latest_ranking.get("trade_date") or latest_ranking.get("date") or ""
        )[:10]
        ranking_prediction_date = str(
            latest_ranking.get("prediction_for_date")
            or latest_ranking.get("prediction_date")
            or ""
        )[:10]
        account = dict(snapshot.get("account") or {})
        account_updated_at = str(account.get("updated_at") or "")
        account_date = account_updated_at[:10]
        account_stale = bool(
            ranking_signal_date
            and (not account_date or account_date < ranking_signal_date)
        )
        return {
            **snapshot,
            "user_id": user_id,
            "profile": profile,
            "profile_complete": bool(has_required_paper_trading_profile(profile)),
            "profile_options": get_classic_user_profile_form_options(),
            "cash_flows": load_paper_cash_flows(
                user_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
            )
            or [],
            "backfill_status": load_paper_backfill_status(user_id, output_dir=self.output_dir) or {},
            "ai_reliability": load_current_ai_reliability_state(
                user_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
            )
            or {},
            "scheduler": scheduler,
            "model_context": {
                "model_name": ACTIVE_MODEL_NAME,
                "model_backend": ACTIVE_MODEL_BACKEND,
                "model_version": ACTIVE_MODEL_VERSION,
                "ranking_signal_date": ranking_signal_date,
                "ranking_prediction_date": ranking_prediction_date,
                "account_updated_at": account_updated_at,
                "account_stale": account_stale,
                "expected_signal_date": str(scheduler.get("expected_signal_date") or ""),
                "public_data_ready": bool(scheduler.get("public_data_ready")),
            },
        }

    @staticmethod
    def _trade_date(value: Any) -> str:
        text = str(value or "").strip()
        try:
            return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ValueError("invalid_trade_date") from exc

    @staticmethod
    def _normalized_code_series(series: Any) -> Any:
        text = series.astype(str).str.strip()
        extracted = text.str.extract(r"(\d{6})", expand=False)
        return extracted.where(extracted.notna(), text).str.zfill(6)

    @staticmethod
    def _normalized_date_series(series: Any) -> Any:
        import pandas as pd

        text = series.astype(str).str.strip()
        digits = text.str.replace(r"[^0-9]", "", regex=True).str.slice(0, 8)
        compact = (
            digits.str.slice(0, 4)
            + "-"
            + digits.str.slice(4, 6)
            + "-"
            + digits.str.slice(6, 8)
        )
        parsed = pd.to_datetime(text, errors="coerce", format="mixed").dt.strftime("%Y-%m-%d")
        return compact.where(digits.str.len() == 8, parsed)

    def _attach_daily_ohlc(self, orders: Any, trade_date: str) -> Any:
        import pandas as pd

        data = orders.copy()
        for column in ("open", "high", "low", "close"):
            if column in data.columns:
                data = data.drop(columns=[column])
            data[column] = None
        data["ohlc_available"] = False
        data["close_available"] = False
        if data.empty or "stock_code" not in data.columns:
            return data

        from application.web_read_service import web_read_service

        market = web_read_service.load_signal_ohlc_data()
        required = {"code", "date", "open", "high", "low", "close"}
        if market is None or getattr(market, "empty", True) or not required.issubset(set(market.columns)):
            market = pd.DataFrame(columns=list(required))

        daily = market.loc[:, ["code", "date", "open", "high", "low", "close"]].copy()
        daily["_history_code"] = self._normalized_code_series(daily["code"])
        daily["_history_date"] = self._normalized_date_series(daily["date"])
        daily = daily[daily["_history_date"] == trade_date]
        daily = daily.drop_duplicates(["_history_code", "_history_date"], keep="last")
        daily = daily.drop(columns=["code", "date", "_history_date"])

        data["_history_code"] = self._normalized_code_series(data["stock_code"])
        data = data.drop(columns=["open", "high", "low", "close", "ohlc_available"]).merge(
            daily,
            how="left",
            on="_history_code",
            validate="many_to_one",
        )
        data["ohlc_available"] = data[["open", "high", "low", "close"]].notna().all(axis=1)
        from application.support.ranker_market_data import load_ranker_close_for_date

        fallback = load_ranker_close_for_date(trade_date).rename(
            columns={"code": "_history_code", "close": "_ranker_close"}
        )
        fallback = fallback.drop(columns=["date"], errors="ignore")
        data = data.merge(fallback, how="left", on="_history_code", validate="many_to_one")
        data["close"] = pd.to_numeric(data["close"], errors="coerce").fillna(
            pd.to_numeric(data["_ranker_close"], errors="coerce")
        )
        data["close_available"] = data["close"].notna()
        data["market_data_status"] = "缺失"
        data.loc[data["close_available"], "market_data_status"] = "仅收盘"
        data.loc[data["ohlc_available"], "market_data_status"] = "完整OHLC"
        return data.drop(columns=["_history_code", "_ranker_close"])

    def _historical_sell_lot_matches(
        self,
        user_id: str,
        selected_date: str,
        order_dates: set[str],
        position_dates: set[str],
        sell_operations: Any,
    ) -> dict[str, dict[str, Any]]:
        """Match sells from the authoritative pre-sell position and recent buys."""

        import math
        import pandas as pd

        frames: list[Any] = []
        if self.db_path:
            try:
                from portfolio.storage import PortfolioStorage

                storage = PortfolioStorage(
                    db_path=self.db_path,
                    output_dir=self.output_dir / "portfolio" / user_id,
                    use_database=True,
                )
                database_orders = storage.repo.list_paper_orders(user_id=user_id)
                if database_orders:
                    frames.append(pd.DataFrame(database_orders))
            except Exception:
                pass

        from portfolio.paper_account import load_daily_order_snapshot

        for historical_date in sorted(
            item for item in order_dates if item <= selected_date
        ):
            snapshot = load_daily_order_snapshot(
                user_id,
                historical_date,
                output_dir=self.output_dir,
            )
            if not snapshot.empty:
                frames.append(snapshot)

        if not frames:
            return {}

        orders = pd.concat(frames, ignore_index=True, sort=False)
        if orders.empty or "stock_code" not in orders.columns:
            return {}

        order_dates_series = self._normalized_date_series(
            orders.get("trade_date", pd.Series("", index=orders.index))
        )
        stock_codes = self._normalized_code_series(orders["stock_code"])
        action = orders.get("action", pd.Series("", index=orders.index)).astype(str).str.lower()
        paper_action = orders.get("paper_action", pd.Series("", index=orders.index)).astype(str).str.lower()
        inferred_action = paper_action.map(
            {
                "paper_buy": "buy",
                "paper_sell": "sell",
                "paper_reduce": "sell",
            }
        )
        action = action.where(action.isin(["buy", "sell"]), inferred_action).fillna("")
        quantity = pd.to_numeric(orders.get("quantity", 0), errors="coerce").fillna(0)

        buys = orders[
            action.eq("buy")
            & (quantity > 0)
            & order_dates_series.notna()
            & (order_dates_series <= selected_date)
        ].copy()
        if buys.empty or sell_operations is None or sell_operations.empty:
            return {}

        buys["_buy_quantity"] = quantity.loc[buys.index]
        buys["trade_date"] = order_dates_series.loc[buys.index]
        buys["stock_code"] = stock_codes.loc[buys.index]
        event_time = buys.get("decision_time", pd.Series("", index=buys.index)).fillna("").astype(str)
        created_at = buys.get("created_at", pd.Series("", index=buys.index)).fillna("").astype(str)
        buys["_event_time"] = event_time.where(event_time.str.strip().ne(""), created_at)
        fallback_id = (
            buys["trade_date"]
            + "_"
            + buys["stock_code"]
            + "_buy"
        )
        order_id = buys.get("order_id", pd.Series("", index=buys.index)).fillna("").astype(str)
        buys["_event_id"] = order_id.where(order_id.str.strip().ne(""), fallback_id)
        buys = buys.drop_duplicates(["_event_id"], keep="first")
        buys = buys.sort_values(
            ["trade_date", "_event_time", "_event_id"],
            kind="stable",
        )

        def number(value: Any) -> float:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return 0.0
            return parsed if math.isfinite(parsed) else 0.0

        from portfolio.paper_account import load_daily_position_snapshot

        sell_codes = {
            str(code or "")
            for code in sell_operations.get(
                "stock_code",
                pd.Series("", index=sell_operations.index),
            )
            if str(code or "")
        }
        prior_dates = sorted(
            (item for item in position_dates if item < selected_date),
            reverse=True,
        )
        position_snapshot_dates_by_stock = {
            code: (prior_dates[0] if prior_dates else "")
            for code in sell_codes
        }
        position_quantities_before = {code: 0.0 for code in sell_codes}
        cycle_start_dates = {code: "" for code in sell_codes}
        pending_cycle_codes = set(sell_codes)
        for snapshot_index, snapshot_date in enumerate(prior_dates):
            snapshot = load_daily_position_snapshot(
                user_id,
                snapshot_date,
                output_dir=self.output_dir,
                fallback=False,
            )
            quantities = {code: 0.0 for code in sell_codes}
            if not snapshot.empty and "stock_code" in snapshot.columns:
                snapshot_codes = self._normalized_code_series(snapshot["stock_code"])
                snapshot_quantities = pd.to_numeric(
                    snapshot.get("quantity", 0),
                    errors="coerce",
                ).fillna(0)
                for code in sell_codes:
                    quantities[code] = float(
                        snapshot_quantities[snapshot_codes.eq(code)].sum()
                    )
            if snapshot_index == 0:
                position_quantities_before.update(quantities)
                for code in list(pending_cycle_codes):
                    if number(quantities.get(code)) <= 1e-9:
                        cycle_start_dates[code] = snapshot_date
                        pending_cycle_codes.remove(code)
            else:
                for code in list(pending_cycle_codes):
                    if number(quantities.get(code)) <= 1e-9:
                        cycle_start_dates[code] = snapshot_date
                        pending_cycle_codes.remove(code)
            if not pending_cycle_codes:
                break

        matches: dict[str, dict[str, Any]] = {}
        for sell in sell_operations.to_dict(orient="records"):
            stock_code = str(sell.get("stock_code") or "")
            sell_id = str(sell.get("trade_record_id") or f"{selected_date}_{stock_code}")
            sell_quantity = number(sell.get("quantity"))
            if not stock_code or sell_quantity <= 0:
                continue

            position_snapshot_date = position_snapshot_dates_by_stock.get(stock_code, "")
            position_quantity_before = number(
                position_quantities_before.get(stock_code)
            )
            cycle_start_date = cycle_start_dates.get(stock_code, "")

            target_position_quantity = (
                position_quantity_before
                if position_quantity_before > 0
                else sell_quantity
            )
            candidate_buys = buys[
                buys["stock_code"].eq(stock_code)
                & buys["trade_date"].le(selected_date)
            ]
            if cycle_start_date:
                candidate_buys = candidate_buys[
                    candidate_buys["trade_date"].gt(cycle_start_date)
                ]

            active_lots_descending: list[dict[str, Any]] = []
            remaining_position_quantity = target_position_quantity
            for buy in reversed(candidate_buys.to_dict(orient="records")):
                if remaining_position_quantity <= 1e-9:
                    break
                original_quantity = number(buy.get("_buy_quantity"))
                if original_quantity <= 0:
                    continue
                active_quantity = min(original_quantity, remaining_position_quantity)
                active_lots_descending.append(
                    {
                        "lot_id": f"{buy.get('trade_date')}_{stock_code}",
                        "trade_date": buy.get("trade_date"),
                        "stock_code": stock_code,
                        "stock_name": buy.get("stock_name"),
                        "executed_price": number(buy.get("executed_price")),
                        "original_quantity": original_quantity,
                        "available_quantity": active_quantity,
                        "original_gross_amount": number(buy.get("gross_amount")),
                        "original_total_fee": number(buy.get("total_fee")),
                    }
                )
                remaining_position_quantity -= active_quantity

            active_lots = list(reversed(active_lots_descending))
            remaining_sell_quantity = sell_quantity
            matched_lots: list[dict[str, Any]] = []
            for lot in active_lots:
                if remaining_sell_quantity <= 1e-9:
                    break
                available_before = number(lot.get("available_quantity"))
                matched_quantity = min(remaining_sell_quantity, available_before)
                remaining_after = available_before - matched_quantity
                original_quantity = number(lot.get("original_quantity"))
                original_fee = number(lot.get("original_total_fee"))
                buy_price = number(lot.get("executed_price"))
                matched_lots.append(
                    {
                        "lot_id": lot.get("lot_id"),
                        "trade_date": lot.get("trade_date"),
                        "stock_code": lot.get("stock_code"),
                        "stock_name": lot.get("stock_name"),
                        "executed_price": buy_price,
                        "original_quantity": original_quantity,
                        "quantity": matched_quantity,
                        "remaining_quantity_before": available_before,
                        "remaining_quantity_after": remaining_after,
                        "gross_amount": buy_price * matched_quantity,
                        "total_fee": (
                            original_fee * matched_quantity / original_quantity
                            if original_quantity > 0
                            else 0.0
                        ),
                    }
                )
                remaining_sell_quantity -= matched_quantity

            matches[sell_id] = {
                "purchase_lots": matched_lots,
                "purchase_lot_count": len(matched_lots),
                "matched_quantity": sell_quantity - remaining_sell_quantity,
                "unmatched_quantity": max(0.0, remaining_sell_quantity),
                "matching_method": "position_snapshot_recent_lots",
                "position_snapshot_date": position_snapshot_date,
                "position_quantity_before": position_quantity_before,
                "cycle_start_date": cycle_start_date,
            }

        return matches

    def daily_history(self, user_id: str, trade_date: str) -> dict[str, Any]:
        import pandas as pd

        from portfolio.paper_account import (
            load_daily_order_snapshot,
            load_daily_position_snapshot,
            load_paper_trading_snapshot,
        )

        user_id = self._user_id(user_id)
        selected_date = self._trade_date(trade_date)
        base_snapshot = load_paper_trading_snapshot(
            user_id,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        position_dates = {
            str(item)
            for item in (base_snapshot.get("position_snapshot_dates") or [])
            if str(item or "").strip()
        }
        order_dates = {
            str(item)
            for item in (base_snapshot.get("order_snapshot_dates") or [])
            if str(item or "").strip()
        }
        available_dates = sorted(position_dates | order_dates, reverse=True)

        if self.db_path is not None:
            from portfolio.storage import PortfolioStorage

            storage = PortfolioStorage(
                db_path=self.db_path,
                output_dir=self.output_dir / "portfolio" / user_id,
                use_database=True,
            )
            database_orders = [
                row
                for row in storage.repo.list_paper_orders(user_id=user_id)
                if str(row.get("trade_date") or "") == selected_date
            ]
            executions = [
                row
                for row in storage.repo.list_strategy_execution_history(user_id)
                if str(row.get("trade_date") or "") == selected_date
            ]
            positions_after = (
                list(executions[-1].get("positions_after") or [])
                if executions
                else []
            )
            positions = pd.DataFrame(positions_after)
            orders = pd.DataFrame(database_orders)
        else:
            positions = load_daily_position_snapshot(
                user_id,
                selected_date,
                output_dir=self.output_dir,
                fallback=False,
            )
            orders = load_daily_order_snapshot(
                user_id,
                selected_date,
                output_dir=self.output_dir,
            )

        if not orders.empty:
            action = orders.get("action", pd.Series("", index=orders.index)).astype(str).str.lower()
            paper_action = orders.get("paper_action", pd.Series("", index=orders.index)).astype(str).str.lower()
            quantity = pd.to_numeric(orders.get("quantity", 0), errors="coerce").fillna(0)
            orders = orders[
                (action.isin(["buy", "sell"]) | paper_action.isin(["paper_buy", "paper_sell", "paper_reduce"]))
                & (quantity > 0)
            ].copy()
        operations = self._attach_daily_ohlc(orders, selected_date)
        if not operations.empty:
            operations = operations.sort_values(["action", "stock_code"], kind="stable")

        actions = operations.get(
            "action",
            pd.Series("", index=operations.index),
        ).astype(str).str.lower()
        paper_actions = operations.get(
            "paper_action",
            pd.Series("", index=operations.index),
        ).astype(str).str.lower()
        inferred_actions = paper_actions.map(
            {
                "paper_buy": "buy",
                "paper_sell": "sell",
                "paper_reduce": "sell",
            }
        )
        actions = actions.where(actions.isin(["buy", "sell"]), inferred_actions).fillna("")
        if not operations.empty:
            operations["action"] = actions
            operation_codes = self._normalized_code_series(operations["stock_code"])
            operations["stock_code"] = operation_codes
            operations["trade_record_id"] = selected_date + "_" + operation_codes

        buy_operations = operations[actions == "buy"].copy()
        sell_operations = operations[actions == "sell"].copy()
        if not sell_operations.empty:
            sell_lot_matches = self._historical_sell_lot_matches(
                user_id,
                selected_date,
                order_dates,
                position_dates,
                sell_operations,
            )
            match_records = [
                sell_lot_matches.get(
                    str(trade_record_id or ""),
                    {
                        "purchase_lots": [],
                        "purchase_lot_count": 0,
                        "matched_quantity": 0.0,
                        "unmatched_quantity": float(quantity or 0.0),
                        "matching_method": "position_snapshot_recent_lots",
                        "position_snapshot_date": "",
                        "position_quantity_before": 0.0,
                        "cycle_start_date": "",
                    },
                )
                for trade_record_id, quantity in zip(
                    sell_operations["trade_record_id"],
                    sell_operations["quantity"],
                )
            ]
            for field in (
                "purchase_lots",
                "purchase_lot_count",
                "matched_quantity",
                "unmatched_quantity",
                "matching_method",
                "position_snapshot_date",
                "position_quantity_before",
                "cycle_start_date",
            ):
                sell_operations[field] = [record[field] for record in match_records]

        ohlc_available = (
            operations.get("ohlc_available", pd.Series(False, index=operations.index))
            .fillna(False)
            .astype(bool)
        )
        close_available = (
            operations.get("close_available", pd.Series(False, index=operations.index))
            .fillna(False)
            .astype(bool)
        )
        return {
            "user_id": user_id,
            "trade_date": selected_date,
            "available_dates": available_dates,
            "has_position_snapshot": selected_date in position_dates,
            "positions": positions,
            "operations": operations,
            "buy_operations": buy_operations,
            "sell_operations": sell_operations,
            "summary": {
                "position_count": int(len(positions)),
                "operation_count": int(len(operations)),
                "buy_count": int((actions == "buy").sum()),
                "sell_count": int((actions == "sell").sum()),
                "ohlc_matched_count": int(ohlc_available.sum()),
                "ohlc_missing_count": int((~ohlc_available).sum()),
                "close_matched_count": int(close_available.sum()),
            },
        }

    def profile(self, user_id: str) -> dict[str, Any]:
        from application.paper_profile_service import (
            get_classic_user_profile_form_options,
            has_required_paper_trading_profile,
            load_classic_user_context,
        )

        user_id = self._user_id(user_id)
        profile = dict(
            load_classic_user_context(
                user_id,
                output_dir=self.output_dir,
                db_path=self.db_path,
            )
            or {}
        )
        return {
            "user_id": user_id,
            "profile": profile,
            "complete": bool(has_required_paper_trading_profile(profile)),
            "options": get_classic_user_profile_form_options(),
        }

    def save_profile(
        self,
        *,
        user_id: str,
        profile: dict[str, Any],
        request_id: str,
        idempotency_key: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("profile_update_confirmation_required")
        if not str(request_id or "").strip():
            raise ValueError("request_id_required")
        if not str(idempotency_key or "").strip():
            raise ValueError("idempotency_key_required")
        user_id = self._user_id(user_id)
        payload = dict(profile or {})
        payload["user_id"] = user_id
        from application.paper_profile_service import save_classic_user_context

        result = save_classic_user_context(
            payload,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        return {
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "status": str((result or {}).get("status") or "saved"),
            "profile": self.profile(user_id)["profile"],
        }

    def proposals(self, user_id: str) -> list[dict[str, Any]]:
        user_id = self._user_id(user_id)
        from agent.session.pending_action_store import load_pending_actions

        actions = load_pending_actions(user_id, self.output_dir)
        allowed_intents = {"capital_change", "paper_backfill"}
        records = [
            self._proposal_summary(dict(plan or {}))
            for plan in actions.values()
            if str((plan or {}).get("intent") or "") in allowed_intents
        ]
        return sorted(records, key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def preview_capital_change(
        self,
        *,
        user_id: str,
        flow_type: str,
        amount: float,
        effective_date: str | None,
        reason: str,
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not str(request_id or "").strip() or not str(idempotency_key or "").strip():
            raise ValueError("request_id_and_idempotency_key_required")
        user_id = self._user_id(user_id)
        normalized_type = str(flow_type or "").strip().lower()
        if normalized_type not in {"deposit", "withdrawal"}:
            raise ValueError("invalid_flow_type")
        if float(amount or 0) <= 0:
            raise ValueError("amount_must_be_positive")
        from agent.session.pending_action_store import get_pending_plan
        from agent.tool_engine import AGENT_MAIN, execute_tool

        result = execute_tool(
            "capital.change.preview",
            {
                "user_id": user_id,
                "flow_type": normalized_type,
                "amount": float(amount),
                "effective_date": str(effective_date or date.today().isoformat()),
                "reason": str(reason or ""),
                "idempotency_key": str(idempotency_key),
            },
            context=self._tool_context(user_id, request_id=request_id),
            agent_type=AGENT_MAIN,
        )
        payload = self._result_payload(result)
        plan_id = str((payload.get("data") or {}).get("plan_id") or "")
        plan = get_pending_plan(user_id, plan_id, self.output_dir) if plan_id else None
        return {
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "result": self._strip_write_secrets(payload),
            "proposal": self._proposal_summary(plan) if plan else None,
        }

    def preview_backfill(
        self,
        *,
        user_id: str,
        start_date: str,
        end_date: str,
        initial_cash: float | None,
        force: bool,
        resume: bool,
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not str(request_id or "").strip() or not str(idempotency_key or "").strip():
            raise ValueError("request_id_and_idempotency_key_required")
        if not str(start_date or "").strip():
            raise ValueError("start_date_required")
        user_id = self._user_id(user_id)
        profile = self.profile(user_id)["profile"]
        capital = float(initial_cash or profile.get("available_capital") or DEFAULT_INITIAL_CASH)
        from agent.session.pending_action_store import get_pending_plan
        from agent.tool_engine import AGENT_MAIN, execute_tool

        result = execute_tool(
            "backfill.preview",
            {
                "user_id": user_id,
                "start_date": str(start_date),
                "end_date": str(end_date or "latest"),
                "initial_cash": capital,
                "resume": bool(resume),
                "force": bool(force),
                "skip_news": False,
                "strategy": "top10_buffer",
                "top_k": 10,
                "entry_top_k": 10,
                "hold_buffer_rank": 15,
                "max_positions": 10,
                "continue_on_error": True,
                "idempotency_key": str(idempotency_key),
            },
            context=self._tool_context(user_id, request_id=request_id),
            agent_type=AGENT_MAIN,
        )
        payload = self._result_payload(result)
        plan_id = str((payload.get("data") or {}).get("plan_id") or "")
        plan = get_pending_plan(user_id, plan_id, self.output_dir) if plan_id else None
        return {
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "result": self._strip_write_secrets(payload),
            "proposal": self._proposal_summary(plan) if plan else None,
        }

    def commit_proposal(
        self,
        *,
        user_id: str,
        plan_id: str,
        confirmation_text: str,
        request_id: str,
        idempotency_key: str,
        allow_long_running: bool = False,
    ) -> dict[str, Any]:
        if not str(request_id or "").strip() or not str(idempotency_key or "").strip():
            raise ValueError("request_id_and_idempotency_key_required")
        user_id = self._user_id(user_id)
        from agent.session.pending_action_store import get_pending_plan

        plan = get_pending_plan(user_id, str(plan_id), self.output_dir)
        if not plan:
            raise ValueError("proposal_not_found")
        if str(plan.get("intent") or "") == "paper_backfill" and not allow_long_running:
            raise ValueError("paper_backfill_requires_task_runtime")
        expected = self._confirmation_phrase(str(plan_id))
        if str(confirmation_text or "").strip().upper() != expected:
            raise ValueError("confirmation_text_mismatch")
        token = str(plan.get("confirmation_token") or "")
        if not token:
            raise ValueError("confirmation_token_missing_on_server")
        from agent.write_gateway import execute_confirmed_plan_v2

        result = execute_confirmed_plan_v2(
            plan_id=str(plan_id),
            confirmation_token=token,
            user_id=user_id,
            conversation_id=f"react-paper-trading:{user_id}",
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        return {
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "result": self._strip_write_secrets(self._result_payload(result)),
            "proposal": self._proposal_summary(
                get_pending_plan(user_id, str(plan_id), self.output_dir) or plan
            ),
        }

    def reject_proposal(
        self,
        *,
        user_id: str,
        plan_id: str,
        reason: str,
        request_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not str(request_id or "").strip() or not str(idempotency_key or "").strip():
            raise ValueError("request_id_and_idempotency_key_required")
        user_id = self._user_id(user_id)
        from agent.session.confirmation_manager import reject_confirmation_plan

        success, status, plan = reject_confirmation_plan(
            user_id,
            str(plan_id),
            output_dir=self.output_dir,
            db_path=self.db_path,
            reason=str(reason or "user_rejected"),
        )
        return {
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "success": bool(success),
            "status": status,
            "proposal": self._proposal_summary(plan) if plan else None,
        }

    def cancel_cash_flow(
        self,
        *,
        user_id: str,
        cash_flow_id: str,
        request_id: str,
        idempotency_key: str,
        confirmed: bool,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("cash_flow_cancel_confirmation_required")
        if not str(request_id or "").strip() or not str(idempotency_key or "").strip():
            raise ValueError("request_id_and_idempotency_key_required")
        user_id = self._user_id(user_id)
        from portfolio.paper_account import cancel_pending_paper_cash_flow

        result = cancel_pending_paper_cash_flow(
            str(cash_flow_id),
            user_id,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        return {
            "request_id": request_id,
            "idempotency_key": idempotency_key,
            "result": result,
        }


web_paper_trading_service = WebPaperTradingApplicationService()
