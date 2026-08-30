from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from database.schemas import json_dumps, json_loads
from database.postgres_store import PostgresStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _code(value: Any) -> str:
    text = str(value or "").split(".")[0]
    return text.zfill(6) if text.isdigit() else text


def _date(value: Any) -> str:
    return str(value or "").split(" ")[0][:10]


def _float_or_none(value: Any) -> float | None:
    try:
        number = float(value)
        return None if number != number else number
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class PredictionRepository:
    """Database authority for live ranking/model-prediction results."""

    def __init__(self, db_path: str | Path | None = None):
        self.store = PostgresStore()

    @staticmethod
    def normalize_ranking_record(record: dict[str, Any], *, source_kind: str = "ranking") -> dict[str, Any]:
        payload = dict(record or {})
        trade_date = _date(payload.get("trade_date") or payload.get("date"))
        predict_for_date = _date(
            payload.get("prediction_for_date")
            or payload.get("predict_for_date")
            or payload.get("prediction_date")
            or payload.get("next_trade_date")
        )
        stock_code = _code(payload.get("stock_code") or payload.get("code") or payload.get("ts_code"))
        model_name = str(payload.get("model_name") or payload.get("model_version") or "unknown")
        rank = _int_or_none(payload.get("pred_rank", payload.get("rank")))
        score = _float_or_none(payload.get("pred_score", payload.get("score")))
        prediction_id = "prediction_" + uuid5(
            NAMESPACE_URL,
            f"runtime-ranking|{source_kind}|{trade_date}|{predict_for_date}|{model_name}|{stock_code}",
        ).hex[:24]
        return {
            "prediction_id": prediction_id,
            "trade_date": trade_date,
            "prediction_for_date": predict_for_date,
            "stock_code": stock_code,
            "stock_name": str(payload.get("stock_name") or payload.get("name") or ""),
            "model_name": model_name,
            "pred_score": score,
            "pred_rank": rank,
            "pred_return": _float_or_none(payload.get("pred_return") or payload.get("pred_5d_ret")),
            "risk_score": _float_or_none(payload.get("risk_score")),
            "risk_level": str(payload.get("risk_level") or ""),
            "confidence": str(payload.get("confidence") or payload.get("confidence_score") or ""),
            "source_kind": str(source_kind or "ranking"),
            "payload_json": json_dumps(payload),
            "created_at": str(payload.get("created_at") or _now()),
            "updated_at": _now(),
        }

    @staticmethod
    def _hydrate(row: dict[str, Any]) -> dict[str, Any]:
        payload = json_loads(row.get("payload_json"), default={})
        result = dict(payload if isinstance(payload, dict) else {})
        result.update(
            {
                "prediction_id": row.get("prediction_id"),
                "trade_date": row.get("trade_date"),
                "date": row.get("trade_date"),
                "prediction_for_date": row.get("prediction_for_date"),
                "predict_for_date": row.get("prediction_for_date"),
                "prediction_date": row.get("prediction_for_date"),
                "stock_code": row.get("stock_code"),
                "code": row.get("stock_code"),
                "stock_name": row.get("stock_name"),
                "name": row.get("stock_name"),
                "model_name": row.get("model_name"),
                "pred_score": row.get("pred_score"),
                "score": row.get("pred_score"),
                "pred_rank": row.get("pred_rank"),
                "rank": row.get("pred_rank"),
                "pred_return": row.get("pred_return"),
                "risk_score": row.get("risk_score"),
                "risk_level": row.get("risk_level"),
                "confidence": row.get("confidence"),
                "source_kind": row.get("source_kind"),
            }
        )
        return result

    def insert_prediction(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        if "payload_json" not in payload:
            explicit_id = str(payload.get("prediction_id") or "")
            payload = self.normalize_ranking_record(
                payload,
                source_kind=str(payload.get("source_kind") or "prediction"),
            )
            if explicit_id:
                payload["prediction_id"] = explicit_id
        return self.store.upsert("model_prediction", payload)

    def replace_snapshot(
        self,
        records: list[dict[str, Any]],
        *,
        source_kind: str = "ranking",
        exclusive_scope: bool = False,
    ) -> list[dict[str, Any]]:
        normalized = [self.normalize_ranking_record(row, source_kind=source_kind) for row in records]
        if not normalized:
            return []
        if any(not row["trade_date"] or not row["stock_code"] for row in normalized):
            raise ValueError("ranking_identity_fields_required")
        snapshot_keys = {
            (str(row["trade_date"]), str(row["model_name"]), str(row["source_kind"]))
            for row in normalized
        }

        with self.store.transaction() as conn:
            if exclusive_scope:
                for trade_date, _model_name, kind in snapshot_keys:
                    self.store.delete_where(
                        "model_prediction",
                        {"trade_date": trade_date, "source_kind": kind},
                        connection=conn,
                    )
            else:
                for trade_date, model_name, kind in snapshot_keys:
                    self.store.delete_where(
                        "model_prediction",
                        {"trade_date": trade_date, "model_name": model_name, "source_kind": kind},
                        connection=conn,
                    )
            for row in normalized:
                self.store.insert("model_prediction", row, connection=conn)

        return normalized

    def get_prediction(self, prediction_id: str) -> dict[str, Any] | None:
        row = self.store.get("model_prediction", {"prediction_id": prediction_id})
        return self._hydrate(row) if row else None

    def list_latest_predictions(
        self,
        *,
        source_kind: str = "ranking",
        stock_code: str | None = None,
        model_name: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["source_kind=%s"]
        params: list[Any] = [str(source_kind)]
        if stock_code:
            clauses.append("stock_code=%s")
            params.append(_code(stock_code))
        if model_name:
            clauses.append("lower(model_name) LIKE %s")
            params.append(f"%{str(model_name).lower()}%")
        where = " AND ".join(clauses)
        sql = f"""SELECT * FROM model_prediction
                  WHERE {where}
                    AND trade_date=(SELECT MAX(trade_date) FROM model_prediction WHERE source_kind=%s)
                  ORDER BY CASE WHEN pred_rank IS NULL THEN 1 ELSE 0 END, pred_rank, pred_score DESC"""
        params.append(str(source_kind))
        if limit is not None:
            sql += " LIMIT %s"
            params.append(max(0, int(limit)))
        rows = self.store.fetch_all(sql, params)
        return [self._hydrate(row) for row in rows]

    def list_predictions(
        self,
        trade_date: str | None = None,
        stock_code: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not trade_date:
            return self.list_latest_predictions(stock_code=stock_code, limit=limit)
        filters: dict[str, Any] = {"trade_date": _date(trade_date)}
        if stock_code:
            filters["stock_code"] = _code(stock_code)
        rows = self.store.list("model_prediction", filters=filters, order_by="pred_rank", limit=limit)
        return [self._hydrate(row) for row in rows]

    def update_prediction(self, prediction_id: str, changes: dict[str, Any]) -> int:
        return self.store.update("model_prediction", {"prediction_id": prediction_id}, changes)


class RecommendationRepository:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.store = PostgresStore()

    def replace_snapshot(self, user_id: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        user = str(user_id or "default")
        now = _now()
        normalized: list[dict[str, Any]] = []
        for source in records:
            payload = dict(source or {})
            trade_date = _date(payload.get("trade_date") or payload.get("date"))
            stock_code = _code(payload.get("stock_code") or payload.get("code"))
            model_name = str(payload.get("model_name") or "")
            recommendation_id = "recommendation_" + uuid5(
                NAMESPACE_URL,
                f"runtime-recommendation|{user}|{trade_date}|{model_name}|{stock_code}",
            ).hex[:24]
            normalized.append(
                {
                    "recommendation_id": recommendation_id,
                    "user_id": user,
                    "trade_date": trade_date,
                    "stock_code": stock_code,
                    "stock_name": str(payload.get("stock_name") or payload.get("name") or ""),
                    "model_name": model_name,
                    "original_rank": _int_or_none(payload.get("original_rank") or payload.get("pred_rank") or payload.get("rank")),
                    "combined_adjustment": _float_or_none(payload.get("combined_adjustment")),
                    "target_weight": _float_or_none(payload.get("target_weight")),
                    "payload_json": json_dumps(payload),
                    "created_at": str(payload.get("created_at") or now),
                    "updated_at": now,
                }
            )
        if not normalized:
            return []
        dates = {str(row["trade_date"]) for row in normalized}
        if len(dates) != 1 or not next(iter(dates)):
            raise ValueError("recommendation_snapshot_trade_date_required")
        trade_date = next(iter(dates))

        with self.store.transaction() as conn:
            self.store.delete_where(
                "portfolio_recommendation_result",
                {"user_id": user, "trade_date": trade_date},
                connection=conn,
            )
            for row in normalized:
                self.store.insert("portfolio_recommendation_result", row, connection=conn)

        return normalized

    def list_latest(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.store.fetch_all(
            """SELECT * FROM portfolio_recommendation_result
               WHERE user_id=%s AND trade_date=(
                   SELECT MAX(trade_date) FROM portfolio_recommendation_result WHERE user_id=%s
               )
               ORDER BY CASE WHEN original_rank IS NULL THEN 1 ELSE 0 END, original_rank""",
            (str(user_id or "default"), str(user_id or "default")),
        )
        output: list[dict[str, Any]] = []
        for row in rows:
            payload = json_loads(row.pop("payload_json", "{}"), default={})
            output.append({**dict(payload or {}), **row})
        return output

    def list_for_date(
        self,
        user_id: str,
        trade_date: str,
    ) -> list[dict[str, Any]]:
        rows = self.store.list(
            "portfolio_recommendation_result",
            filters={
                "user_id": str(user_id or "default"),
                "trade_date": _date(trade_date),
            },
            order_by="original_rank",
        )
        output: list[dict[str, Any]] = []
        for row in rows:
            payload = json_loads(row.pop("payload_json", "{}"), default={})
            output.append({**dict(payload or {}), **row})
        return output


class RuntimeDataImportAuditRepository:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.store = PostgresStore()

    def upsert(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        payload["details_json"] = json.dumps(payload.pop("details", {}), ensure_ascii=False, sort_keys=True)
        return self.store.upsert("runtime_data_import_audit", payload)

    def find(self, source_kind: str, source_sha256: str) -> dict[str, Any] | None:
        rows = self.store.list(
            "runtime_data_import_audit",
            filters={"source_kind": str(source_kind), "source_sha256": str(source_sha256)},
            limit=1,
        )
        return rows[0] if rows else None
