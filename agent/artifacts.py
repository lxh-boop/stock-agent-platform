from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from database.repositories import AgentRepository
from runtime_paths import get_runtime_dir
from storage_governance.lifecycle import maybe_prune_expired_artifact_payloads


ARTIFACT_SCHEMA_VERSION = "artifact-v1"
SENSITIVE_KEYS = {
    "api_key",
    "token",
    "confirmation_token",
    "confirmation_token_hash",
    "authorization",
    "authorization_header",
    "cookie",
    "password",
    "secret",
    "tushare_token",
    "llm_api_key",
}


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    artifact_type: str
    schema_version: str
    user_id: str
    project_id: str = "stock_daily_app"
    conversation_id: str = ""
    run_id: str = ""
    task_id: str = ""
    trade_date: str = ""
    producer_type: str = "tool"
    producer_id: str = ""
    content: dict[str, Any] = field(default_factory=dict)
    content_summary: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    sensitivity: str = "normal"
    visibility_scope: str = "same_user_conversation_or_run"
    created_at: str = ""
    expires_at: str = ""
    contract: str = "tool.result"
    version: str = "1.0"
    schema_id: str = "ToolResult.v1"
    provenance: dict[str, Any] = field(default_factory=dict)
    contracts: list[dict[str, Any]] = field(default_factory=list)
    content_hash: str = ""
    status: str = "active"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                out[str(key)] = "***"
            else:
                out[str(key)] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _sanitize(value: Any, max_chars: int = 800) -> Any:
    value = _redact(value)
    if isinstance(value, dict):
        return {str(key): _sanitize(item, max_chars=max_chars) for key, item in value.items()}
    if isinstance(value, list):
        items = [_sanitize(item, max_chars=max_chars) for item in value[:20]]
        if len(value) > 20:
            items.append({"truncated_count": len(value) - 20})
        return items
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "...[truncated]"
    return value


def _artifact_root(output_dir: str | Path | None = None) -> Path:
    if output_dir:
        root = Path(output_dir) / "artifacts"
    else:
        root = get_runtime_dir() / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def artifact_cache_key(intent: str, arguments: dict[str, Any] | None) -> str:
    payload = {
        "intent": str(intent or ""),
        "arguments": _sanitize(_redact(dict(arguments or {})), max_chars=2000),
    }
    return _hash(payload)


def resolve_current_trade_date() -> str:
    # 解析当前所属交易日（YYYY-MM-DD）：优先用交易日历取最近交易日，失败时退化为当天日期
    try:
        from scheduler.trading_calendar import get_latest_trading_day

        return get_latest_trading_day(datetime.now()).isoformat()
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def infer_artifact_outputs(
    intent: str,
    result: dict[str, Any] | None = None,
    *,
    output_contracts: list[Any] | tuple[Any, ...] | None = None,
) -> list[str]:
    from agent.capability_index import OUTPUTS_BY_TOOL

    outputs = set(OUTPUTS_BY_TOOL.get(str(intent or ""), set()))
    outputs.update(
        str(getattr(item, "slot_id", "") or "")
        for item in (output_contracts or [])
        if str(getattr(item, "slot_id", "") or "")
    )
    data = dict((result or {}).get("data") or {})
    if data.get("positions"):
        outputs.update({"portfolio_state", "position_count"})
    if data.get("records"):
        outputs.update({"market_evidence", "candidate_stocks"})
    if data.get("chunks") or data.get("events") or data.get("mcp_sources"):
        outputs.update({"evidence", "market_evidence"})
    if data.get("plan_id"):
        outputs.update({"operation_preview", "confirmation_request"})
    return sorted(outputs)


def build_artifact_from_result(
    *,
    user_id: str,
    run_id: str,
    conversation_id: str = "",
    task_id: str = "",
    producer_type: str = "tool",
    producer_id: str = "",
    artifact_type: str = "tool_result",
    result: dict[str, Any] | None = None,
    sources: list[dict[str, Any]] | None = None,
    ttl_minutes: int = 180,
    output_contracts: list[Any] | tuple[Any, ...] | None = None,
    provenance: dict[str, Any] | None = None,
    trade_date: str = "",
) -> Artifact:
    result = _redact(dict(result or {}))
    created_at = _now_text()
    trade_date = str(trade_date or "") or resolve_current_trade_date()
    expires_at = (datetime.now() + timedelta(minutes=max(1, int(ttl_minutes or 180)))).strftime("%Y-%m-%d %H:%M:%S")
    contract_rows: list[dict[str, Any]] = []
    for item in output_contracts or []:
        descriptor = item.contract_descriptor() if hasattr(item, "contract_descriptor") else {}
        if not isinstance(descriptor, dict) or not descriptor:
            continue
        contract_rows.append(
            {
                "slot_id": str(getattr(item, "slot_id", "") or ""),
                "contract": str(descriptor.get("contract") or ""),
                "version": str(descriptor.get("version") or "1.0"),
                "schema_id": str(descriptor.get("schema_id") or ""),
            }
        )
    primary = contract_rows[0] if contract_rows else {
        "slot_id": "tool_result",
        "contract": "tool.result",
        "version": "1.0",
        "schema_id": "ToolResult.v1",
    }
    artifact_provenance = {
        "producer_type": str(producer_type or "tool"),
        "producer_id": str(producer_id or ""),
        "run_id": str(run_id or ""),
        "task_id": str(task_id or ""),
        **dict(provenance or {}),
    }
    summary = _sanitize(
        {
            "success": bool(result.get("success")),
            "message": str(result.get("message") or "")[:300],
            "errors": result.get("errors") or [],
            "warnings": result.get("warnings") or [],
            "produced_outputs": infer_artifact_outputs(
                producer_id,
                result,
                output_contracts=output_contracts,
            ),
            "contract": primary["contract"],
            "version": primary["version"],
            "schema_id": primary["schema_id"],
        },
        max_chars=1200,
    )
    content = {
        "result": result,
        "produced_outputs": summary.get("produced_outputs") or [],
        "contracts": contract_rows,
        "provenance": artifact_provenance,
    }
    content_hash = _hash(content)
    return Artifact(
        artifact_id=f"artifact_{uuid4().hex[:12]}",
        artifact_type=artifact_type,
        schema_version=ARTIFACT_SCHEMA_VERSION,
        user_id=str(user_id or "default"),
        conversation_id=str(conversation_id or ""),
        run_id=str(run_id or ""),
        task_id=str(task_id or ""),
        trade_date=trade_date,
        producer_type=producer_type,
        producer_id=str(producer_id or ""),
        content=content,
        content_summary=summary,
        sources=[_sanitize(item, max_chars=800) for item in (sources or []) if isinstance(item, dict)],
        sensitivity="normal",
        visibility_scope="same_user_conversation_or_run",
        created_at=created_at,
        expires_at=expires_at,
        contract=str(primary["contract"]),
        version=str(primary["version"]),
        schema_id=str(primary["schema_id"]),
        provenance=artifact_provenance,
        contracts=contract_rows,
        content_hash=content_hash,
        status="active",
    )


class ArtifactStore:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        output_dir: str | Path | None = None,
    ) -> None:
        self.repo = AgentRepository(db_path)
        self.output_dir = output_dir

    def save(self, artifact: Artifact) -> dict[str, Any]:
        root = _artifact_root(self.output_dir)
        user_dir = root / artifact.user_id
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / f"{artifact.artifact_id}.json"
        payload = artifact.to_dict()
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        db_run_id: str | None = artifact.run_id or None
        if db_run_id and not self.repo.store.get("agent_runs", {"run_id": db_run_id}):
            db_run_id = None
        self.repo.upsert_artifact(
            {
                "artifact_id": artifact.artifact_id,
                "user_id": artifact.user_id,
                "run_id": db_run_id,
                "artifact_type": artifact.artifact_type,
                "path": str(path),
                "content_hash": artifact.content_hash,
                "size_bytes": path.stat().st_size,
                "retention_policy": "standard",
                "created_at": artifact.created_at,
                "expires_at": artifact.expires_at,
                "metadata": {
                    "schema_version": artifact.schema_version,
                    "project_id": artifact.project_id,
                    "conversation_id": artifact.conversation_id,
                    "task_id": artifact.task_id,
                    "trade_date": artifact.trade_date,
                    "producer_type": artifact.producer_type,
                    "producer_id": artifact.producer_id,
                    "content_summary": artifact.content_summary,
                    "sources": artifact.sources,
                    "sensitivity": artifact.sensitivity,
                    "visibility_scope": artifact.visibility_scope,
                    "contract": artifact.contract,
                    "version": artifact.version,
                    "schema_id": artifact.schema_id,
                    "provenance": artifact.provenance,
                    "contracts": artifact.contracts,
                    "status": artifact.status,
                    "produced_outputs": artifact.content.get("produced_outputs") or [],
                },
            }
        )
        # Physical payload retention is separate from the short reuse TTL.
        # GC is best-effort and throttled; an artifact save must never fail because cleanup failed.
        try:
            maybe_prune_expired_artifact_payloads(root)
        except Exception:
            pass
        return {
            "artifact_id": artifact.artifact_id,
            "artifact_type": artifact.artifact_type,
            "path": str(path),
            "content_hash": artifact.content_hash,
            "contract": artifact.contract,
            "version": artifact.version,
            "schema_id": artifact.schema_id,
            "provenance": artifact.provenance,
            "contracts": artifact.contracts,
            "produced_outputs": artifact.content.get("produced_outputs") or [],
        }

    def read(
        self,
        artifact_id: str,
        *,
        user_id: str,
        conversation_id: str = "",
        run_id: str = "",
    ) -> dict[str, Any] | None:
        row = self.repo._decode_runtime_record(
            "artifacts",
            self.repo.store.get("artifacts", {"artifact_id": str(artifact_id or "")}),
        )
        if not row or str(row.get("user_id") or "") != str(user_id or ""):
            return None
        metadata = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else {}
        row_conversation = str(metadata.get("conversation_id") or "")
        row_run_id = str(row.get("run_id") or "")
        same_conversation = bool(conversation_id and row_conversation == str(conversation_id))
        linked_run = bool(run_id and row_run_id == str(run_id))
        if row_conversation and not (same_conversation or linked_run):
            return None
        path = Path(str(row.get("path") or ""))
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list_readable(
        self,
        *,
        user_id: str,
        conversation_id: str = "",
        run_id: str = "",
        artifact_type: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        filters: dict[str, Any] = {"user_id": str(user_id or "")}
        if artifact_type:
            filters["artifact_type"] = artifact_type
        rows = self.repo._list_runtime(
            "artifacts",
            filters=filters,
            order_by="created_at",
            descending=True,
            limit=limit,
        )
        readable: list[dict[str, Any]] = []
        now = datetime.now()
        for row in rows:
            metadata = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else {}
            expires_at = str(row.get("expires_at") or "")
            if expires_at:
                try:
                    if datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S") < now:
                        continue
                except ValueError:
                    pass
            row_conversation = str(metadata.get("conversation_id") or "")
            row_run_id = str(row.get("run_id") or "")
            if row_conversation and conversation_id and row_conversation != str(conversation_id) and row_run_id != str(run_id or ""):
                continue
            readable.append(
                {
                    "artifact_id": row.get("artifact_id"),
                    "artifact_type": row.get("artifact_type"),
                    "run_id": row.get("run_id"),
                    "content_hash": row.get("content_hash"),
                    "created_at": row.get("created_at"),
                    "expires_at": row.get("expires_at"),
                    **metadata,
                }
            )
        return readable

    def find_reusable(
        self,
        *,
        user_id: str,
        conversation_id: str = "",
        run_id: str = "",
        producer_id: str = "",
        produced_outputs: list[str] | None = None,
    ) -> dict[str, Any] | None:
        wanted = {str(item) for item in (produced_outputs or []) if str(item).strip()}
        rows = self.list_readable(
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            artifact_type="tool_result",
            limit=50,
        )
        for row in rows:
            if producer_id and str(row.get("producer_id") or "") != str(producer_id):
                continue
            outputs = set(row.get("produced_outputs") or [])
            if wanted and not wanted <= outputs:
                continue
            return row
        return None


def save_tool_result_artifact(
    *,
    db_path: str | Path | None,
    output_dir: str | Path | None = None,
    user_id: str,
    run_id: str,
    conversation_id: str = "",
    task_id: str = "",
    tool_name: str,
    result: dict[str, Any] | None,
    sources: list[dict[str, Any]] | None = None,
    output_contracts: list[Any] | tuple[Any, ...] | None = None,
    provenance: dict[str, Any] | None = None,
    trade_date: str = "",
) -> dict[str, Any]:
    artifact = build_artifact_from_result(
        user_id=user_id,
        run_id=run_id,
        conversation_id=conversation_id,
        task_id=task_id,
        producer_type="tool",
        producer_id=tool_name,
        artifact_type="tool_result",
        result=result,
        sources=sources,
        output_contracts=output_contracts,
        provenance=provenance,
        trade_date=trade_date,
    )
    return ArtifactStore(db_path=db_path, output_dir=output_dir).save(artifact)
