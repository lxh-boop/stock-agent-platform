from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from database.schemas import json_dumps
from database.postgres_store import PostgresStore
from strategies.adapters.hierarchical_top10_strategy import (
    HierarchicalTop10Strategy,
)


VALID_STATUSES = {
    "draft",
    "generated",
    "validation_failed",
    "validated",
    "backtesting",
    "backtested",
    "approved",
    "registered_disabled",
    "enabled",
    "disabled",
    "archived",
}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class StrategyManifest:
    strategy_id: str
    strategy_name: str
    version: str
    source_type: str
    module_path: str
    class_name: str
    config_schema: dict[str, Any] = field(default_factory=dict)
    status: str = "draft"
    created_by: str = "system"
    created_at: str = field(default_factory=_now_text)
    code_hash: str | None = None
    validation_status: str = "not_run"
    backtest_status: str = "not_run"
    enabled_for_paper_trading: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_changes(self, **changes: Any) -> "StrategyManifest":
        payload = self.to_dict()
        payload.update(changes)
        return StrategyManifest(**payload)


def built_in_hierarchical_top10_manifest() -> StrategyManifest:
    strategy = HierarchicalTop10Strategy()
    return StrategyManifest(
        strategy_id=strategy.strategy_id,
        strategy_name=strategy.strategy_name,
        version=strategy.version,
        source_type="builtin_adapter",
        module_path="strategies.adapters.hierarchical_top10_strategy",
        class_name="HierarchicalTop10Strategy",
        config_schema=strategy.get_config_schema(),
        status="enabled",
        created_by="system",
        code_hash=None,
        validation_status="passed",
        backtest_status="uses_existing_pipeline",
        enabled_for_paper_trading=True,
        metadata={"adapter_for": "portfolio.hierarchical_top10_allocator"},
    )


class StrategyRegistry:
    def __init__(
        self,
        output_dir: str | Path = "outputs",
        db_path: str | Path | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.db_path = db_path

    @property
    def registry_path(self) -> Path:
        return self.output_dir / "strategies" / "strategy_registry.json"

    def _read_file_manifests(self) -> list[StrategyManifest]:
        path = self.registry_path
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        rows = raw if isinstance(raw, list) else raw.get("strategies", [])
        result: list[StrategyManifest] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                result.append(StrategyManifest(**row))
            except TypeError:
                continue
        return result

    def _write_file_manifests(self, manifests: list[StrategyManifest]) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [manifest.to_dict() for manifest in manifests]
        self.registry_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _persist_db(self, manifest: StrategyManifest) -> None:
        record = {
            "strategy_id": manifest.strategy_id,
            "version": manifest.version,
            "strategy_name": manifest.strategy_name,
            "source_type": manifest.source_type,
            "module_path": manifest.module_path,
            "class_name": manifest.class_name,
            "config_schema_json": json_dumps(manifest.config_schema),
            "status": manifest.status,
            "created_by": manifest.created_by,
            "created_at": manifest.created_at,
            "code_hash": manifest.code_hash or "",
            "validation_status": manifest.validation_status,
            "backtest_status": manifest.backtest_status,
            "enabled_for_paper_trading": 1 if manifest.enabled_for_paper_trading else 0,
            "enabled_at": _now_text() if manifest.enabled_for_paper_trading else "",
            "disabled_at": "" if manifest.enabled_for_paper_trading else _now_text(),
            "archived_at": _now_text() if manifest.status == "archived" else "",
            "previous_strategy_id": str(manifest.metadata.get("previous_strategy_id") or ""),
            "previous_version": str(manifest.metadata.get("previous_version") or ""),
            "metadata_json": json_dumps(manifest.metadata),
        }
        try:
            PostgresStore().upsert("strategy_registry", record)
        except Exception:
            return

    def list(self, include_archived: bool = False) -> list[StrategyManifest]:
        file_manifests = self._read_file_manifests()
        built_in = built_in_hierarchical_top10_manifest()
        if any(item.enabled_for_paper_trading for item in file_manifests):
            built_in = built_in.with_changes(
                status="disabled",
                enabled_for_paper_trading=False,
            )
        manifests = [built_in]
        seen = {(built_in.strategy_id, built_in.version)}
        for manifest in file_manifests:
            marker = (manifest.strategy_id, manifest.version)
            if marker in seen:
                continue
            seen.add(marker)
            manifests.append(manifest)
        if include_archived:
            return manifests
        return [item for item in manifests if item.status != "archived"]

    def get(self, strategy_id: str, version: str | None = None) -> StrategyManifest | None:
        matches = [
            item
            for item in self.list(include_archived=True)
            if item.strategy_id == strategy_id
        ]
        if version:
            for item in matches:
                if item.version == version:
                    return item
            return None
        enabled = [item for item in matches if item.enabled_for_paper_trading]
        if enabled:
            return enabled[-1]
        return matches[-1] if matches else None

    def register(
        self,
        manifest: StrategyManifest,
        *,
        allow_existing: bool = False,
    ) -> StrategyManifest:
        status = manifest.status if manifest.status in VALID_STATUSES else "draft"
        manifest = manifest.with_changes(status=status)
        existing = self._read_file_manifests()
        marker = (manifest.strategy_id, manifest.version)
        if (
            marker in {(item.strategy_id, item.version) for item in existing}
            and not allow_existing
        ):
            raise ValueError("strategy_version_already_registered")
        if allow_existing:
            existing = [
                item
                for item in existing
                if (item.strategy_id, item.version) != marker
            ]
        existing.append(manifest)
        self._write_file_manifests(existing)
        self._persist_db(manifest)
        return manifest

    def enable(self, strategy_id: str, version: str) -> StrategyManifest:
        manifests = self._read_file_manifests()
        found: StrategyManifest | None = None
        updated: list[StrategyManifest] = []
        for item in manifests:
            if item.strategy_id == strategy_id and item.version == version:
                found = item.with_changes(
                    status="enabled",
                    enabled_for_paper_trading=True,
                )
                updated.append(found)
            elif item.enabled_for_paper_trading:
                updated.append(
                    item.with_changes(
                        status="disabled",
                        enabled_for_paper_trading=False,
                    )
                )
            else:
                updated.append(item)
        if found is None:
            built_in = built_in_hierarchical_top10_manifest()
            if built_in.strategy_id == strategy_id and built_in.version == version:
                return built_in
            raise ValueError("strategy_version_not_found")
        self._write_file_manifests(updated)
        for manifest in updated:
            self._persist_db(manifest)
        return found

    def disable(self, strategy_id: str, version: str) -> StrategyManifest:
        manifests = self._read_file_manifests()
        found: StrategyManifest | None = None
        updated: list[StrategyManifest] = []
        for item in manifests:
            if item.strategy_id == strategy_id and item.version == version:
                found = item.with_changes(
                    status="disabled",
                    enabled_for_paper_trading=False,
                )
                updated.append(found)
            else:
                updated.append(item)
        if found is None:
            raise ValueError("strategy_version_not_found")
        self._write_file_manifests(updated)
        self._persist_db(found)
        return found

    def archive(self, strategy_id: str, version: str) -> StrategyManifest:
        manifest = self.disable(strategy_id, version)
        archived = manifest.with_changes(status="archived")
        existing = [
            item
            for item in self._read_file_manifests()
            if not (item.strategy_id == strategy_id and item.version == version)
        ]
        existing.append(archived)
        self._write_file_manifests(existing)
        self._persist_db(archived)
        return archived

    def rollback_version(self, strategy_id: str) -> StrategyManifest:
        candidates = [
            item
            for item in self._read_file_manifests()
            if item.strategy_id == strategy_id and item.status != "archived"
        ]
        if not candidates:
            raise ValueError("strategy_version_not_found")
        candidates.sort(key=lambda item: item.created_at)
        target = candidates[-2] if len(candidates) >= 2 else candidates[-1]
        return self.enable(target.strategy_id, target.version)


def get_strategy_registry(
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
) -> StrategyRegistry:
    return StrategyRegistry(output_dir=output_dir, db_path=db_path)
