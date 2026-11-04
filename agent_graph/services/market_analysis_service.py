from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from agent_graph.domain_tools._common import (
    first_present,
    latest_trade_date,
    normalize_stock_code as _normalize_stock_code,
    safe_int,
)
from agent_graph.services.top_k import DEFAULT_TOOL_TOP_K, resolve_requested_top_k
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult
from database.repositories import (
    PredictionRepository as DatabasePredictionRepository,
    RecommendationRepository as DatabaseRecommendationRepository,
)


CLASSIC_AI_COLUMNS = [
    "trade_date",
    "stock_code",
    "stock_name",
    "pred_score",
    "pred_rank",
    "confidence",
    "risk_score",
    "news_adjustment",
    "user_adjustment",
    "effective_news_adjustment",
    "combined_adjustment",
    "original_target_weight",
    "ai_reliability_weight",
    "position_adjustment_ratio",
    "target_weight",
    "ai_adjustment_score",
    "ai_adjustment_effect_status",
    "triggered_rules",
    "evidence_news_ids",
    "evidence_chunk_ids",
    "reason",
    "risk_warning",
]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (pd.Timestamp,)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if pd.isna(value) if not isinstance(value, (dict, list, tuple)) else False:
        return None
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _jsonable(value.item())
        except Exception:
            pass
    return value


def _records_from_df(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return [
        {str(key): _jsonable(item) for key, item in row.items()}
        for row in df.to_dict(orient="records")
    ]


def _row_code(row: dict[str, Any]) -> str:
    return _normalize_stock_code(first_present(row, ["stock_code", "code", "ts_code"], ""))


def _row_name(row: dict[str, Any]) -> str:
    return str(first_present(row, ["stock_name", "name", "asset_name"], ""))


def _find_stock_row(records: list[dict[str, Any]], stock_query: str) -> dict[str, Any] | None:
    query = str(stock_query or "").strip()
    query_code = _normalize_stock_code(query)
    if query_code:
        for row in records:
            if _row_code(row) == query_code:
                return row
    if query:
        for row in records:
            if query in _row_name(row):
                return row
    return None


class RankingRepository:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = db_path

    def _predictions(self, db_path: str | Path | None = None) -> DatabasePredictionRepository:
        return DatabasePredictionRepository(db_path if db_path is not None else self.db_path)

    def _recommendations(self, db_path: str | Path | None = None) -> DatabaseRecommendationRepository:
        return DatabaseRecommendationRepository(db_path if db_path is not None else self.db_path)

    def ranking_path(self, output_dir: str | Path = "outputs", ranking_path: str | Path | None = None) -> Path:
        del output_dir
        if ranking_path:
            raise RuntimeError("runtime_csv_source_disabled:ranking")
        return Path("database") / "model_prediction"

    def recommendation_paths(
        self,
        user_id: str = "default",
        output_dir: str | Path = "outputs",
        recommendations_path: str | Path | None = None,
    ) -> list[Path]:
        del user_id, output_dir
        if recommendations_path:
            raise RuntimeError("runtime_csv_source_disabled:recommendations")
        return [Path("database") / "portfolio_recommendation_result"]

    def load_latest_ranking(
        self,
        output_dir: str | Path = "outputs",
        *,
        ranking_path: str | Path | None = None,
        limit: int | None = None,
        db_path: str | Path | None = None,
        model_name: str | None = None,
    ) -> list[dict[str, Any]]:
        self.ranking_path(output_dir, ranking_path)
        return self._predictions(db_path).list_latest_predictions(
            limit=limit, model_name=model_name
        )

    def load_latest_recommendations(
        self,
        user_id: str = "default",
        output_dir: str | Path = "outputs",
        *,
        recommendations_path: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        self.recommendation_paths(user_id, output_dir, recommendations_path)
        return self._recommendations(db_path).list_latest(user_id)

    def load_ranking_frame(
        self,
        output_dir: str | Path = "outputs",
        *,
        ranking_path: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> pd.DataFrame:
        return pd.DataFrame(
            self.load_latest_ranking(output_dir, ranking_path=ranking_path, db_path=db_path)
        )

    def load_recommendations_frame(
        self,
        user_id: str = "default",
        output_dir: str | Path = "outputs",
        *,
        recommendations_path: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> pd.DataFrame:
        return pd.DataFrame(
            self.load_latest_recommendations(
                user_id,
                output_dir,
                recommendations_path=recommendations_path,
                db_path=db_path,
            )
        )


class StockMetadataRepository:
    def resolve_stock_name(self, records: list[dict[str, Any]], stock_query: str) -> str:
        row = _find_stock_row(records, stock_query) or {}
        return _row_name(row)


class PredictionRepository(RankingRepository):
    def load_model_predictions(
        self, output_dir: str | Path = "outputs", *, db_path: str | Path | None = None
    ) -> list[dict[str, Any]]:
        return self.load_latest_ranking(output_dir, db_path=db_path)


class ScoreRepository(RankingRepository):
    def load_latest_scores(
        self, output_dir: str | Path = "outputs", *, db_path: str | Path | None = None
    ) -> list[dict[str, Any]]:
        return self.load_latest_ranking(output_dir, db_path=db_path)


class MarketAnalysisService:
    def __init__(
        self,
        *,
        ranking_repository: RankingRepository | None = None,
        stock_metadata_repository: StockMetadataRepository | None = None,
        prediction_repository: PredictionRepository | None = None,
        score_repository: ScoreRepository | None = None,
    ) -> None:
        self.ranking_repository = ranking_repository or RankingRepository()
        self.stock_metadata_repository = stock_metadata_repository or StockMetadataRepository()
        self.prediction_repository = prediction_repository or PredictionRepository()
        self.score_repository = score_repository or ScoreRepository()

    def normalize_stock_code(self, value: Any) -> str:
        return _normalize_stock_code(value)

    def resolve_stock_name(
        self,
        stock_query: str,
        *,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> str:
        return self.stock_metadata_repository.resolve_stock_name(
            self.ranking_repository.load_latest_ranking(output_dir, db_path=db_path),
            stock_query,
        )

    def load_latest_scores(
        self, output_dir: str | Path = "outputs", *, db_path: str | Path | None = None
    ) -> list[dict[str, Any]]:
        return self.score_repository.load_latest_scores(output_dir, db_path=db_path)

    def load_model_predictions(
        self, output_dir: str | Path = "outputs", *, db_path: str | Path | None = None
    ) -> list[dict[str, Any]]:
        return self.prediction_repository.load_model_predictions(output_dir, db_path=db_path)

    def _source(self, label: str, path: str | Path) -> dict[str, Any]:
        path = Path(path)
        normalized = str(path).replace("\\", "/")
        if normalized.startswith("database/"):
            return {
                "label": label,
                "source_type": "database",
                "table": normalized.split("/", 1)[1],
                "exists": True,
            }
        return {"label": label, "path": str(path), "exists": path.exists()}

    def _filter_model_name(self, rows: list[dict[str, Any]], model_name: str | None) -> list[dict[str, Any]]:
        wanted = str(model_name or "").strip().lower()
        if not wanted:
            return rows
        filtered = [
            row
            for row in rows
            if wanted in str(row.get("model_name") or "").lower()
            or str(row.get("model_name") or "").lower() in wanted
        ]
        return filtered or rows

    def get_ranking(
        self,
        stock_code: str | None = None,
        top_k: int | str | None = 50,
        output_dir: str | Path = "outputs",
        *,
        ranking_path: str | Path | None = None,
        model_name: str | None = None,
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        path = self.ranking_repository.ranking_path(output_dir, ranking_path)
        requested_top_k = resolve_requested_top_k(
            task_top_k=top_k,
            tool_default_top_k=DEFAULT_TOOL_TOP_K,
        )
        direct_limited_read = not stock_code and not model_name and str(top_k or "").lower() not in {"all", "all_rows"}
        rows = self.ranking_repository.load_latest_ranking(
            output_dir,
            ranking_path=ranking_path,
            limit=requested_top_k if direct_limited_read else None,
            db_path=db_path,
            model_name=model_name,
        )
        rows = self._filter_model_name(rows, model_name)
        normalized = self.normalize_stock_code(stock_code)
        if normalized:
            filtered = [row for row in rows if _row_code(row) == normalized]
        elif str(top_k or "").lower() in {"all", "all_rows"}:
            filtered = rows
        else:
            filtered = rows[:requested_top_k]
        as_of_date = latest_trade_date(rows) if rows else ""
        sources = [self._source("ranking_latest", path)]
        summary = {
            "total_count": len(rows),
            "returned_count": len(filtered),
            "stock_code": normalized,
            "top_k": top_k if str(top_k or "").lower() in {"all", "all_rows"} else requested_top_k,
            "model_name": model_name or "",
            "source_read_limit": requested_top_k if direct_limited_read else None,
        }
        status = "success" if rows else "missing_ranking"
        data = {
            "records": filtered,
            "summary": summary,
            "sources": sources,
            "as_of_date": as_of_date,
            "not_executed": True,
            "total_count": len(rows),
            "returned_count": len(filtered),
        }
        return {
            "success": bool(rows),
            "status": status,
            "message": "Latest ranking queried." if rows else "Latest ranking is unavailable.",
            "data": data,
            "total_count": len(rows),
            "returned_count": len(filtered),
            "records": filtered,
            "summary": summary,
            "sources": sources,
            "as_of_date": as_of_date,
            "not_executed": True,
            "tool_name": "market.get_ranking",
        }

    def get_latest_ranking_report(
        self,
        *,
        topk: int = 10,
        model_name: str | None = None,
        output_dir: str | Path = "outputs",
        ranking_path: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        path = self.ranking_repository.ranking_path(output_dir, ranking_path)
        df = self.ranking_repository.load_ranking_frame(
            output_dir, ranking_path=ranking_path, db_path=db_path
        )
        if df.empty:
            return {
                "success": False,
                "message": "数据库中未找到最新预测排名，请先运行每日更新任务。",
                "records": [],
            }
        try:
            from ranking_schema import normalize_ranking_columns

            df = normalize_ranking_columns(df)
        except Exception:
            df = df.copy()
        if "code" not in df.columns and "stock_code" in df.columns:
            df["code"] = df["stock_code"]
        if "code" not in df.columns:
            return {
                "success": False,
                "message": "ranking rows are missing the stock code field.",
                "records": [],
            }
        if model_name and "model_name" in df.columns:
            wanted = str(model_name).strip().lower()
            filtered = df[df["model_name"].astype(str).str.lower().map(lambda value: wanted in value or value in wanted)]
            if not filtered.empty:
                df = filtered
        if "rank" in df.columns:
            df = df.sort_values("rank", ascending=True)
        elif "score" in df.columns:
            df = df.sort_values("score", ascending=False)
        df = df.reset_index(drop=True)
        topk = max(1, int(topk or 10))
        show = df.head(topk).copy()
        trade_date = ""
        predict_for_date = ""
        if "date" in df.columns and not df.empty:
            dates = pd.to_datetime(df["date"], errors="coerce").dropna()
            if not dates.empty:
                trade_date = str(dates.max().date())
        for col in ["predict_for_date", "prediction_date", "next_trade_date"]:
            if col in df.columns and not df.empty:
                values = df[col].dropna()
                if not values.empty:
                    predict_for_date = str(values.iloc[0]).split(" ")[0]
                    break
        rename_map = {"code": "stock_code", "name": "stock_name", "date": "trade_date"}
        keep_cols = [
            "rank",
            "code",
            "name",
            "score",
            "confidence_score",
            "confidence",
            "risk_score",
            "risk_level",
            "model_name",
            "date",
            "close",
            "up_prob",
            "up_prob_calibrated",
        ]
        show = show[[col for col in keep_cols if col in show.columns]].rename(columns=rename_map)
        if "stock_code" in show.columns:
            show["stock_code"] = show["stock_code"].astype(str).str.split(".").str[0].str.zfill(6)
        if "predict_for_date" not in show.columns:
            show["predict_for_date"] = predict_for_date
        records = _records_from_df(show)
        return {
            "success": True,
            "message": f"Ranking query succeeded, returned {len(show)} records.",
            "source_file": str(path),
            "total_rows": int(len(df)),
            "topk": int(topk),
            "trade_date": trade_date,
            "predict_for_date": predict_for_date,
            "model_name": model_name or (str(df["model_name"].iloc[0]) if "model_name" in df.columns else ""),
            "records": records,
            "data": {
                "records": records,
                "summary": {"total_count": int(len(df)), "returned_count": len(records), "top_k": int(topk)},
                "sources": [self._source("ranking_latest", path)],
                "as_of_date": trade_date,
                "not_executed": True,
            },
            "tool_name": "market.get_ranking",
        }

    def lookup_stock(
        self,
        stock_query: str,
        user_id: str = "default",
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        ranking = self.ranking_repository.load_latest_ranking(output_dir, db_path=db_path)
        recommendations = self.ranking_repository.load_latest_recommendations(
            user_id, output_dir, db_path=db_path
        )
        ranking_row = _find_stock_row(ranking, stock_query)
        recommendation_row = _find_stock_row(recommendations, stock_query)
        row = recommendation_row or ranking_row or {}
        code = _row_code(row) or self.normalize_stock_code(stock_query)
        stock_name = _row_name(row)
        rank_value = safe_int(first_present(ranking_row or {}, ["rank", "ranking"], None), None)
        as_of_date = latest_trade_date(recommendations or ranking) if (recommendations or ranking) else ""
        sources = [
            self._source("ranking_latest", self.ranking_repository.ranking_path(output_dir)),
            *[
                self._source("recommendations", path)
                for path in self.ranking_repository.recommendation_paths(user_id, output_dir)
            ][:1],
        ]
        payload = {
            "found": bool(row),
            "stock_code": code,
            "stock_name": stock_name,
            "ranking_row": ranking_row or {},
            "recommendation_row": recommendation_row or {},
            "in_ranking": bool(ranking_row),
            "in_recommendations": bool(recommendation_row),
            "rank": rank_value,
            "ranking_count": len(ranking),
            "recommendation_count": len(recommendations),
            "records": [row] if row else [],
            "summary": {
                "found": bool(row),
                "stock_code": code,
                "stock_name": stock_name,
                "rank": rank_value,
            },
            "sources": sources,
            "as_of_date": as_of_date,
            "not_executed": True,
        }
        return {
            "success": bool(row),
            "message": "Stock lookup completed." if row else "Stock was not found in local ranking or recommendations.",
            "data": dict(payload),
            "tool_name": "market.lookup_stock",
            **payload,
        }

    def analyze_stock(
        self,
        user_id: str,
        stock_code: str,
        as_of_date: str | None = None,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        top_k: int = 50,
        include_rag: bool = True,
        *,
        tool_name: str = "market.analyze_stock",
    ) -> ToolResult:
        from agent_graph.domain_tools.stock_analysis_tool import _analyze_stock_impl

        result = _analyze_stock_impl(
            user_id,
            stock_code,
            as_of_date=as_of_date,
            output_dir=output_dir,
            db_path=db_path,
            top_k=top_k,
            include_rag=include_rag,
        )
        data = dict(result.data or {})
        trade_date = str(data.get("trade_date") or as_of_date or "")
        sources = [
            self._source("ranking_latest", self.ranking_repository.ranking_path(output_dir)),
            *[
                self._source("recommendations", path)
                for path in self.ranking_repository.recommendation_paths(user_id, output_dir)
            ][:1],
        ]
        data.update(
            {
                "records": [dict(result.data or {})] if result.data else [],
                "summary": {
                    "stock_code": data.get("stock_code") or self.normalize_stock_code(stock_code),
                    "stock_name": data.get("stock_name") or "",
                    "original_rank": data.get("original_rank"),
                    "combined_adjustment": data.get("combined_adjustment"),
                    "position_adjustment_ratio": data.get("position_adjustment_ratio"),
                },
                "sources": sources,
                "as_of_date": trade_date,
                "not_executed": True,
            }
        )
        return ToolResult(
            success=bool(result.success),
            message=str(result.message or ""),
            data=data,
            warnings=list(result.warnings or []),
            errors=list(result.errors or []),
            permission=ToolPermission.READ,
            tool_name=tool_name,
            disclaimer=result.disclaimer,
            status=result.status,
        )

    def compare_stocks(
        self,
        stock_codes: list[str] | str,
        *,
        user_id: str = "default",
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
        top_k: int = 50,
    ) -> dict[str, Any]:
        if isinstance(stock_codes, str):
            items = [item.strip() for item in stock_codes.replace(";", ",").split(",") if item.strip()]
        else:
            items = [str(item).strip() for item in stock_codes if str(item).strip()]
        records: list[dict[str, Any]] = []
        warnings: list[str] = []
        for code in items:
            analysis = self.analyze_stock(
                user_id,
                code,
                output_dir=output_dir,
                db_path=db_path,
                top_k=top_k,
                include_rag=False,
            )
            if analysis.success:
                records.append(dict(analysis.data or {}))
            else:
                warnings.extend(analysis.errors or [analysis.message])
        summary = {
            "requested_count": len(items),
            "returned_count": len(records),
            "stock_codes": [item.get("stock_code") for item in records],
        }
        return {
            "success": bool(records),
            "message": "Stock comparison completed." if records else "No comparable stock records were found.",
            "data": {
                "records": records,
                "summary": summary,
                "sources": [self._source("ranking_latest", self.ranking_repository.ranking_path(output_dir))],
                "as_of_date": latest_trade_date(records) if records else "",
                "not_executed": True,
            },
            "warnings": warnings,
            "tool_name": "market.compare_stocks",
        }

    def _canonical_ranking(self, ranking: pd.DataFrame) -> pd.DataFrame:
        if ranking is None or ranking.empty:
            return ranking
        df = ranking.copy()
        df["stock_code"] = df.get("stock_code", df.get("code", "")).astype(str).str.split(".").str[0].str.zfill(6)
        df["stock_name"] = df.get("stock_name", df.get("name", ""))
        df["trade_date"] = df.get("trade_date", df.get("date", ""))
        if "date" in df.columns:
            signal_date = pd.to_datetime(df["date"], errors="coerce")
            trade_date = pd.to_datetime(df["trade_date"], errors="coerce")
            same_day = trade_date.notna() & signal_date.notna() & (trade_date == signal_date)
            if same_day.any():
                df.loc[same_day, "trade_date"] = (signal_date[same_day] + pd.offsets.BDay(1)).dt.strftime("%Y-%m-%d")
        df["pred_rank"] = pd.to_numeric(df.get("pred_rank", df.get("rank", None)), errors="coerce")
        df["pred_score"] = pd.to_numeric(df.get("pred_score", df.get("score", 0.0)), errors="coerce")
        if "risk_score" not in df.columns:
            df["risk_score"] = None
        return df

    def _canonical_recommendations(self, recommendations: pd.DataFrame) -> pd.DataFrame:
        if recommendations is None or recommendations.empty:
            return recommendations
        df = recommendations.copy()
        df["stock_code"] = df.get("stock_code", df.get("code", "")).astype(str).str.split(".").str[0].str.zfill(6)
        return df

    def get_signal_summary(
        self,
        output_dir: str | Path = ".",
        *,
        user_id: str = "default",
        ranking_path: str | Path | None = None,
        recommendations_path: str | Path | None = None,
        sort_by: str = "original_rank",
        include_dataframe: bool = False,
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        ranking_file = self.ranking_repository.ranking_path(output_dir, ranking_path)
        ranking = self.ranking_repository.load_ranking_frame(
            output_dir, ranking_path=ranking_path, db_path=db_path
        )
        if ranking.empty:
            empty = pd.DataFrame(columns=CLASSIC_AI_COLUMNS + ["has_ai_adjustment"])
            data = {
                "records": [],
                "summary": {"total_count": 0, "has_ai_adjustment_count": 0},
                "sources": [self._source("ranking_latest", ranking_file)],
                "as_of_date": "",
                "not_executed": True,
            }
            if include_dataframe:
                data["dataframe"] = empty
            return {"success": False, "message": "Latest ranking is unavailable.", "data": data, "tool_name": "market.get_signal_summary"}

        left = self._canonical_ranking(ranking)
        recommendations = self._canonical_recommendations(
            self.ranking_repository.load_recommendations_frame(
                user_id,
                output_dir,
                recommendations_path=recommendations_path,
                db_path=db_path,
            )
        )
        if recommendations.empty:
            merged = left.copy()
            merged["has_ai_adjustment"] = False
        else:
            ai_cols = [col for col in CLASSIC_AI_COLUMNS if col in recommendations.columns]
            merged = left.merge(recommendations[ai_cols], on="stock_code", how="left")
            merged["has_ai_adjustment"] = (
                merged.get("position_adjustment_ratio").notna()
                if "position_adjustment_ratio" in merged.columns
                else False
            )
        for col in CLASSIC_AI_COLUMNS:
            if col not in merged.columns:
                merged[col] = ""
        sort_key = str(sort_by)
        if sort_key == "combined_adjustment" and "combined_adjustment" in merged.columns:
            merged = merged.sort_values("combined_adjustment", ascending=False, na_position="last")
        elif sort_key == "original_score":
            merged = merged.sort_values("pred_score", ascending=False, na_position="last")
        else:
            merged = merged.sort_values("pred_rank", ascending=True, na_position="last")
        merged = merged.reset_index(drop=True)
        as_of_date = ""
        if "trade_date" in merged.columns and not merged.empty:
            values = [str(item)[:10] for item in merged["trade_date"].tolist() if str(item).strip()]
            as_of_date = sorted(values)[-1] if values else ""
        rec_sources = [
            self._source("recommendations", path)
            for path in self.ranking_repository.recommendation_paths(user_id, output_dir, recommendations_path)
        ][:1]
        data = {
            "records": _records_from_df(merged),
            "summary": {
                "total_count": int(len(merged)),
                "has_ai_adjustment_count": int(merged["has_ai_adjustment"].fillna(False).astype(bool).sum()),
                "sort_by": sort_key,
            },
            "sources": [self._source("ranking_latest", ranking_file), *rec_sources],
            "as_of_date": as_of_date,
            "not_executed": True,
        }
        if include_dataframe:
            data["dataframe"] = merged
        return {
            "success": True,
            "message": "Signal summary loaded.",
            "data": data,
            "tool_name": "market.get_signal_summary",
        }

    def build_score_explanation(
        self,
        stock_query: str,
        *,
        user_id: str = "default",
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> dict[str, Any]:
        lookup = self.lookup_stock(
            stock_query, user_id=user_id, output_dir=output_dir, db_path=db_path
        )
        row = dict(lookup.get("ranking_row") or lookup.get("recommendation_row") or {})
        return {
            "success": bool(row),
            "message": "Score explanation built." if row else "No local score row was found.",
            "data": {
                "records": [row] if row else [],
                "summary": lookup.get("summary") or {},
                "sources": lookup.get("sources") or [],
                "as_of_date": lookup.get("as_of_date") or "",
                "not_executed": True,
            },
            "tool_name": "market.build_score_explanation",
        }


market_analysis_service = MarketAnalysisService()
