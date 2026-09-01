from __future__ import annotations

import hashlib
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any


class WebReadApplicationService:
    """Read-only application facade used by the React migration.

    The facade composes existing application services. It never writes settings,
    starts a backtest, persists a monitor snapshot, or mutates portfolio state.
    """

    @staticmethod
    def _text(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _event_id(row: dict[str, Any]) -> str:
        raw = "|".join(
            str(row.get(key) or "")
            for key in ("code", "date", "publish_time", "source", "title")
        )
        return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:20]

    @staticmethod
    def _safe_number(value: Any) -> float | int | None:
        try:
            number = float(value)
            if math.isnan(number) or math.isinf(number):
                return None
            return int(number) if number.is_integer() else number
        except Exception:
            return None

    def ranking(self) -> Any:
        from application.dashboard_service import dashboard_service

        return dashboard_service.load_ranking()

    def metrics(self) -> Any:
        from application.dashboard_service import dashboard_service

        return dashboard_service.load_metrics()

    def selected_strategy(self) -> dict[str, Any]:
        from application.model_search_service import load_selected_strategy

        value = load_selected_strategy()
        return value if isinstance(value, dict) else {}

    def dashboard_summary(self) -> dict[str, Any]:
        from application.dashboard_service import dashboard_service

        ranking = self.ranking()
        nav, backtest_metrics, _trades, _predictions = dashboard_service.load_backtest_outputs()
        events = dashboard_service.load_news_events_for_app()
        selected = self.selected_strategy()
        local_cfg = self.public_settings()
        snapshot = dashboard_service.get_ranking_file_snapshot()
        return {
            "ranking": {
                "available": ranking is not None and not getattr(ranking, "empty", True),
                "total": int(len(ranking)) if ranking is not None else 0,
                "signal_date": snapshot.get("signal_date") or "",
                "prediction_date": snapshot.get("prediction_date") or "",
                "updated_at": snapshot.get("mtime_text") if snapshot.get("exists") else None,
            },
            "model": {
                "backend": local_cfg.get("model_backend"),
                "version": local_cfg.get("model_version"),
                "selected_strategy": selected,
                "metrics_available": self.metrics() is not None,
            },
            "backtest": {
                "available": nav is not None and not getattr(nav, "empty", True),
                "metrics": backtest_metrics or {},
            },
            "news": {
                "available": not getattr(events, "empty", True),
                "total": int(len(events)) if events is not None else 0,
            },
            "feature_flags": local_cfg.get("feature_flags", {}),
        }

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
        is_compact_date = digits.str.len() == 8
        compact = (
            digits.str.slice(0, 4)
            + "-"
            + digits.str.slice(4, 6)
            + "-"
            + digits.str.slice(6, 8)
        )
        parsed = pd.to_datetime(text.where(~is_compact_date), errors="coerce").dt.strftime("%Y-%m-%d")
        return compact.where(is_compact_date, parsed)

    def load_signal_ohlc_data(self) -> Any:
        from application.dashboard_service import dashboard_service

        return dashboard_service.load_latest_raw_data()

    def _attach_signal_date_ohlc(self, ranking: Any) -> Any:
        import pandas as pd

        data = ranking.copy()
        existing_close = pd.to_numeric(
            data.get("close", pd.Series(index=data.index, dtype=float)),
            errors="coerce",
        )
        for column in ("open", "high", "low", "close"):
            if column in data.columns:
                data = data.drop(columns=[column])
            data[column] = None
        data["_ranking_close"] = existing_close
        data["ohlc_available"] = False
        if "code" not in data.columns or "date" not in data.columns:
            data["close"] = data["_ranking_close"]
            data["close_source"] = data["close"].notna().map(
                {True: "model_panel_close", False: "missing"}
            )
            return data.drop(columns=["_ranking_close"])

        raw = self.load_signal_ohlc_data()
        required = {"code", "date", "open", "high", "low", "close"}
        if raw is None or getattr(raw, "empty", True) or not required.issubset(set(raw.columns)):
            data["close"] = data["_ranking_close"]
            data["close_source"] = data["close"].notna().map(
                {True: "model_panel_close", False: "missing"}
            )
            return data.drop(columns=["_ranking_close"])

        market = raw.loc[:, ["code", "date", "open", "high", "low", "close"]].copy()
        market["_signal_code"] = self._normalized_code_series(market["code"])
        market["_signal_date"] = self._normalized_date_series(market["date"])
        market = market.drop_duplicates(["_signal_code", "_signal_date"], keep="last")
        market = market.drop(columns=["code", "date"])

        data["_signal_code"] = self._normalized_code_series(data["code"])
        data["_signal_date"] = self._normalized_date_series(data["date"])
        data = data.drop(columns=["open", "high", "low", "close", "ohlc_available"]).merge(
            market,
            how="left",
            on=["_signal_code", "_signal_date"],
            validate="many_to_one",
        )
        data["ohlc_available"] = data[["open", "high", "low", "close"]].notna().all(axis=1)
        market_close = pd.to_numeric(data["close"], errors="coerce")
        data["close"] = market_close.fillna(data["_ranking_close"])
        data["close_source"] = "missing"
        data.loc[data["_ranking_close"].notna(), "close_source"] = "model_panel_close"
        data.loc[market_close.notna(), "close_source"] = "signal_ohlc"
        return data.drop(columns=["_signal_code", "_signal_date", "_ranking_close"])

    def ranking_page(self, *, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        import pandas as pd

        metrics = self.metrics()
        validation = metrics.get("historical_validation", {}) if isinstance(metrics, dict) else {}
        target_validation = None
        if isinstance(validation, dict) and validation.get("valid_test_days"):
            target_validation = {
                "valid_test_days": self._safe_number(validation.get("valid_test_days")),
                "test_start_date": self._text(validation.get("test_start_date")),
                "test_end_date": self._text(validation.get("test_end_date")),
                "train_end_date": self._text(validation.get("train_end_date")),
                "best_epoch": self._safe_number(validation.get("best_epoch")),
                "universe_next_day_up_probability": self._safe_number(
                    validation.get("universe_next_day_up_probability")
                ),
                "top5_next_day_up_probability": self._safe_number(
                    validation.get("top5_next_day_up_probability")
                ),
                "top10_next_day_up_probability": self._safe_number(
                    validation.get("top10_next_day_up_probability")
                ),
                "top15_next_day_up_probability": self._safe_number(
                    validation.get("top15_next_day_up_probability")
                ),
                "top5_lift_vs_universe": self._safe_number(
                    validation.get("top5_lift_vs_universe")
                ),
                "top10_lift_vs_universe": self._safe_number(
                    validation.get("top10_lift_vs_universe")
                ),
                "top15_lift_vs_universe": self._safe_number(
                    validation.get("top15_lift_vs_universe")
                ),
                "all_topk_above_universe": bool(
                    validation.get("all_topk_above_universe")
                ),
                "target_precision": self._safe_number(validation.get("target_precision")),
                "target_met": bool(validation.get("target_met")),
                "promotion_status": self._text(validation.get("promotion_status")),
            }
        frame = self.ranking()
        if frame is None or getattr(frame, "empty", True):
            return {
                "records": [],
                "total": 0,
                "offset": int(offset),
                "limit": int(limit),
                "top15_statistics": None,
                "target_validation": target_validation,
            }
        data = self._attach_signal_date_ohlc(frame)
        numeric_columns = {
            "rank", "pred_rank", "score", "pred_score", "raw_score", "model_score",
            "member_1_raw_score", "member_1_rank_pct", "member_2_raw_score",
            "member_2_rank_pct", "historical_rank_bucket_up_rate", "close", "open",
            "high", "low", "amount", "volume", "pct_chg", "ret_5", "ret_20",
            "vol_20", "drawdown_20", "risk_score", "confidence_score",
        }
        for column in numeric_columns.intersection(data.columns):
            data[column] = pd.to_numeric(data[column], errors="coerce")
        if "pred_score" not in data.columns:
            data["pred_score"] = None
        for source_column in ("raw_score", "model_score", "prediction_score", "pred_5d_ret"):
            if source_column in data.columns:
                data["pred_score"] = data["pred_score"].where(
                    data["pred_score"].notna(),
                    data[source_column],
                )
        if "rank" in data.columns:
            data = data.sort_values("rank", ascending=True)
        elif "score" in data.columns:
            data = data.sort_values("score", ascending=False)
        top15_statistics = None
        if "top15_daily_average_up_rate" in data.columns:
            available = data[data["top15_daily_average_up_rate"].notna()]
            if not available.empty:
                summary = available.iloc[0]
                top15_statistics = {
                    "top5_daily_average_up_rate": self._safe_number(
                        summary.get("top5_daily_average_up_rate")
                    ),
                    "top10_daily_average_up_rate": self._safe_number(
                        summary.get("top10_daily_average_up_rate")
                    ),
                    "daily_average_up_rate": self._safe_number(
                        summary.get("top15_daily_average_up_rate")
                    ),
                    "observation_days": self._safe_number(
                        summary.get("top15_observation_days")
                    ),
                    "complete_days": self._safe_number(
                        summary.get("top15_complete_days")
                    ),
                    "observation_count": self._safe_number(
                        summary.get("top15_observation_count")
                    ),
                    "rise_count": self._safe_number(
                        summary.get("top15_rise_count")
                    ),
                    "top_k": self._safe_number(summary.get("calibration_top_k")),
                    "start_date": self._text(summary.get("top15_start_date")),
                    "end_date": self._text(summary.get("top15_end_date")),
                    "target": self._text(summary.get("calibration_target")),
                }
        start = max(int(offset), 0)
        size = min(max(int(limit), 1), 500)
        return {
            "records": data.iloc[start : start + size],
            "total": int(len(data)),
            "offset": start,
            "limit": size,
            "top15_statistics": top15_statistics,
            "target_validation": target_validation,
        }

    def data_freshness(self) -> list[dict[str, Any]]:
        import config

        items = [
            ("ranking", "最新排名", getattr(config, "RANKING_LATEST_PATH", "")),
            ("metrics", "主动排名模型指标", getattr(config, "ACTIVE_RANKING_MODEL_METRICS_PATH", "")),
            ("raw_data", "最新行情", getattr(config, "LATEST_RAW_DATA_PATH", "")),
            ("feature_data", "最新特征（按需计算）", getattr(config, "LATEST_RAW_DATA_PATH", "")),
            ("ranker_features", "截面排名特征缓存", getattr(config, "ACTIVE_RANKING_FEATURE_DIR", "")),
            ("news", "新闻缓存", getattr(config, "NEWS_CACHE_PATH", "")),
            ("announcement", "公告缓存", getattr(config, "ANNOUNCEMENT_CACHE_PATH", "")),
            ("rag_documents", "RAG 文档", getattr(config, "RAG_DOCUMENTS_PATH", "")),
            ("rag_index", "RAG 索引", getattr(config, "RAG_INDEX_PATH", "")),
            ("backtest_nav", "回测净值", getattr(config, "BACKTEST_NAV_PATH", "")),
        ]
        rows: list[dict[str, Any]] = []
        for key, label, raw_path in items:
            path = Path(raw_path)
            exists = bool(raw_path) and path.exists()
            stat = path.stat() if exists else None
            rows.append(
                {
                    "key": key,
                    "label": label,
                    "status": "ready" if exists else "missing",
                    "updated_at": (
                        datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat()
                        if stat is not None
                        else None
                    ),
                    "size_bytes": (None if key == "feature_data" else int(stat.st_size) if stat is not None and path.is_file() else None),
                }
            )
        return rows

    def stock_detail(self, stock_code: str) -> dict[str, Any]:
        from application.dashboard_service import dashboard_service

        code = str(stock_code).zfill(6)
        ranking = self.ranking()
        ranking_record: dict[str, Any] = {}
        if ranking is not None and not getattr(ranking, "empty", True) and "code" in ranking.columns:
            found = ranking[ranking["code"].astype(str).str.zfill(6) == code]
            if not found.empty:
                ranking_record = found.iloc[0].to_dict()
        raw = dashboard_service.load_latest_raw_data()
        market_record: dict[str, Any] = {}
        if raw is not None and not getattr(raw, "empty", True) and "code" in raw.columns:
            found = raw[raw["code"].astype(str).str.zfill(6) == code]
            if not found.empty:
                if "date" in found.columns:
                    found = found.sort_values("date")
                market_record = found.iloc[-1].to_dict()
        events = dashboard_service.load_news_events_for_app()
        event_count = 0
        if events is not None and not getattr(events, "empty", True) and "code" in events.columns:
            event_count = int((events["code"].astype(str).str.zfill(6) == code).sum())
        return {
            "stock_code": code,
            "name": ranking_record.get("name") or market_record.get("name") or "",
            "ranking": ranking_record,
            "market": market_record,
            "event_count": event_count,
            "found": bool(ranking_record or market_record),
        }

    def stock_history(self, stock_code: str, *, limit: int = 120) -> dict[str, Any]:
        from application.dashboard_service import dashboard_service

        code = str(stock_code).zfill(6)
        frame = dashboard_service.load_latest_raw_data()
        if frame is None or getattr(frame, "empty", True) or "code" not in frame.columns:
            return {"stock_code": code, "records": [], "total": 0}
        data = frame[frame["code"].astype(str).str.zfill(6) == code].copy()
        if "date" in data.columns:
            data = data.sort_values("date")
        size = min(max(int(limit), 1), 1000)
        data = data.tail(size)
        return {"stock_code": code, "records": data, "total": int(len(data))}

    def stock_evidence(self, stock_code: str, *, query: str = "", top_k: int = 10) -> dict[str, Any]:
        from agent.services.evidence_service import evidence_service

        code = str(stock_code).zfill(6)
        search_query = self._text(query) or f"{code} 最新新闻 公告 风险"
        try:
            result = evidence_service.search_documents(
                search_query,
                stock_code=code,
                top_k=min(max(int(top_k), 1), 50),
            )
            data = dict(result.get("data") or {})
            records = list(data.get("records") or [])
            return {
                "stock_code": code,
                "query": search_query,
                "records": records,
                "total": len(records),
                "warning": ";".join(result.get("warnings") or []),
            }
        except Exception as exc:
            return {
                "stock_code": code,
                "query": search_query,
                "records": [],
                "total": 0,
                "warning": type(exc).__name__,
            }

    def stock_explanation(self, stock_code: str) -> dict[str, Any]:
        from llm_explainer import load_cached_ai_explanation

        detail = self.stock_detail(stock_code)
        source = dict(detail.get("ranking") or {})
        source.update({key: value for key, value in (detail.get("market") or {}).items() if key not in source})
        cached: Any = None
        if source:
            try:
                cached = load_cached_ai_explanation(source)
            except Exception:
                cached = None
        return {
            "stock_code": str(stock_code).zfill(6),
            "available": cached not in (None, "", {}),
            "cached": cached,
            "generated": False,
            "message": "阶段 6.2 只读取已有解释缓存，不触发 LLM 生成。",
        }

    def model_catalog(self) -> dict[str, Any]:
        from application.dashboard_service import dashboard_service

        frame = dashboard_service.load_model_zoo_table()
        return {"records": frame, "total": int(len(frame))}

    def model_search_results(self) -> dict[str, Any]:
        from application import model_search_service as service

        def table(path: Any) -> Any:
            try:
                return service.load_table_file(path)
            except Exception:
                return []

        return {
            "candidates": table(service.MODEL_CANDIDATES_PATH),
            "master_backtests": table(service.BACKTEST_MASTER_TABLE_PATH),
            "target_results": table(service.MODEL_SEARCH_RESULTS_PATH),
            "errors": table(service.MODEL_SEARCH_ERRORS_PATH),
            "selected_strategy": service.load_selected_strategy(),
            "discovery_report": service.load_model_discovery_report(),
            "read_only": True,
        }

    def backtest_bundle(self) -> dict[str, Any]:
        from application.dashboard_service import dashboard_service

        nav, metrics, trades, predictions = dashboard_service.load_backtest_outputs()
        return {
            "backtest_id": "latest",
            "available": nav is not None and not getattr(nav, "empty", True),
            "metrics": metrics or {},
            "equity": nav if nav is not None else [],
            "trades": trades if trades is not None else [],
            "predictions": predictions if predictions is not None else [],
        }

    def news_events(
        self,
        *,
        stock_code: str | None = None,
        event_type: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        from application.dashboard_service import classify_event_title, dashboard_service

        frame = dashboard_service.load_news_events_for_app()
        if frame is None or getattr(frame, "empty", True):
            return {"records": [], "total": 0, "offset": int(offset), "limit": int(limit)}
        data = frame.copy()
        if "code" in data.columns:
            data["code"] = data["code"].astype(str).str.zfill(6)
        if stock_code and "code" in data.columns:
            data = data[data["code"] == str(stock_code).zfill(6)]
        if "date" in data.columns:
            parsed = data["date"].map(lambda value: str(value)[:10])
            if start_date:
                data = data[parsed >= str(start_date)]
                parsed = data["date"].map(lambda value: str(value)[:10])
            if end_date:
                data = data[parsed <= str(end_date)]
        normalized_type = self._text(event_type).lower()
        if normalized_type and normalized_type not in {"all", "全部"} and "title" in data.columns:
            def label(title: Any) -> str:
                flags = classify_event_title(str(title or "")) or {}
                if int(flags.get("is_risk_event") or 0) > 0:
                    return "risk"
                if int(flags.get("is_negative_event") or 0) > 0:
                    return "negative"
                if int(flags.get("is_positive_event") or 0) > 0:
                    return "positive"
                return "neutral"
            labels = data["title"].map(label)
            data = data[labels == normalized_type]
        sort_cols = [name for name in ("publish_time", "date") if name in data.columns]
        if sort_cols:
            data = data.sort_values(sort_cols, ascending=False)
        total = int(len(data))
        start = max(int(offset), 0)
        size = min(max(int(limit), 1), 500)
        data = data.iloc[start : start + size].copy()
        records = data.to_dict(orient="records")
        for record in records:
            record["event_id"] = self._event_id(record)
        return {"records": records, "total": total, "offset": start, "limit": size}

    def news_event(self, event_id: str) -> dict[str, Any] | None:
        page = self.news_events(limit=500)
        for record in page["records"]:
            if record.get("event_id") == event_id:
                return record
        return None

    def public_settings(self) -> dict[str, Any]:
        from application.web_settings_service import web_settings_service

        return web_settings_service.public_settings()

    def monitor_summary(self, *, user_id: str = "default") -> dict[str, Any]:
        from application.system_monitor_service import build_system_monitor_snapshot

        result = build_system_monitor_snapshot(user_id=str(user_id or "default"))
        return result.to_dict() if hasattr(result, "to_dict") else dict(result or {})

    def monitor_services(self, *, user_id: str = "default") -> dict[str, Any]:
        from application.system_monitor_service import (
            build_handoff_health_summary,
            build_memory_store_health_summary,
            build_message_bus_health_summary,
            build_react_health_summary,
            build_reflection_health_summary,
        )

        def safe(callable_obj: Any, **kwargs: Any) -> dict[str, Any]:
            try:
                value = callable_obj(**kwargs)
                return value if isinstance(value, dict) else {"status": "ok", "value": value}
            except TypeError:
                try:
                    value = callable_obj()
                    return value if isinstance(value, dict) else {"status": "ok", "value": value}
                except Exception as exc:
                    return {"status": "unavailable", "error": type(exc).__name__}
            except Exception as exc:
                return {"status": "unavailable", "error": type(exc).__name__}

        return {
            "message_bus": safe(build_message_bus_health_summary, user_id=user_id),
            "memory": safe(build_memory_store_health_summary, user_id=user_id),
            "react": safe(build_react_health_summary, user_id=user_id),
            "reflection": safe(build_reflection_health_summary, user_id=user_id),
            "handoff": safe(build_handoff_health_summary, user_id=user_id),
        }

    def monitor_history(self, *, user_id: str = "default", limit: int = 30) -> list[dict[str, Any]]:
        from application.system_monitor_service import list_system_monitor_history

        return list_system_monitor_history(user_id=str(user_id or "default"), limit=min(max(int(limit), 1), 200))

    def monitor_alerts(self, *, user_id: str = "default", limit: int = 100) -> list[dict[str, Any]]:
        from application.system_monitor_service import list_system_monitor_alerts

        return list_system_monitor_alerts(user_id=str(user_id or "default"), limit=min(max(int(limit), 1), 500))


web_read_service = WebReadApplicationService()

__all__ = ["WebReadApplicationService", "web_read_service"]
