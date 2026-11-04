from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

from agent_graph.services.strategy_code_generation_service import (
    StrategyCodeGenerationService,
)
from agent_graph.services.strategy_config_compiler import StrategyConfigCompiler
from agent_graph.services.strategy_proposal_service import StrategyProposalService
from agent_graph.services.strategy_validation_service import StrategyValidationService
from database.repositories.strategy_workflow_repository import (
    StrategyWorkflowRepository,
)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _safe_segment(value: str) -> str:
    cleaned = "".join(
        character
        for character in str(value or "")
        if character.isalnum() or character in {"-", "_", "."}
    )
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("invalid_strategy_artifact_path_segment")
    return cleaned


@dataclass(frozen=True)
class StrategyImplementation:
    implementation_id: str
    proposal_id: str
    proposal_version: int
    user_id: str
    account_id: str
    conversation_id: str
    implementation_type: str
    artifact_root: str
    implementation_hash: str
    artifact_manifest_hash: str
    status: str
    created_at: str
    updated_at: str
    artifact_manifest: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StrategyImplementationService:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        runtime_dir: str | Path = "runtime",
    ) -> None:
        self.db_path = db_path
        self.runtime_dir = Path(runtime_dir)
        self.proposals = StrategyProposalService(db_path)
        self.repository = StrategyWorkflowRepository(db_path)
        self.compiler = StrategyConfigCompiler()
        self.generator = StrategyCodeGenerationService()
        self.validator = StrategyValidationService()

    @property
    def drafts_root(self) -> Path:
        return self.runtime_dir / "strategy_drafts"

    def lock_and_prepare(
        self,
        *,
        proposal_id: str,
        proposal_version: int,
        user_id: str,
        account_id: str,
        conversation_id: str,
        run_id: str,
    ) -> StrategyImplementation:
        proposal = self.proposals.get(proposal_id, user_id=user_id)
        if proposal is None:
            raise ValueError("proposal_not_found")
        if (
            proposal.account_id != account_id
            or proposal.conversation_id != conversation_id
        ):
            raise PermissionError("proposal_scope_mismatch")
        if proposal.current_version != int(proposal_version):
            raise ValueError("stale_proposal_version")

        existing = self.repository.get_proposal_implementation(
            proposal_id,
            proposal_version,
            user_id=user_id,
        )
        if existing:
            return self._from_record(existing)

        if proposal.status != "locked_for_implementation":
            self.proposals.set_status(
                proposal_id,
                user_id=user_id,
                status="locked_for_implementation",
                expected_version=proposal_version,
            )
        return self.prepare_locked(
            proposal_id=proposal_id,
            proposal_version=proposal_version,
            user_id=user_id,
            account_id=account_id,
            conversation_id=conversation_id,
            run_id=run_id,
        )

    def prepare_locked(
        self,
        *,
        proposal_id: str,
        proposal_version: int,
        user_id: str,
        account_id: str,
        conversation_id: str,
        run_id: str,
    ) -> StrategyImplementation:
        proposal = self.proposals.get(proposal_id, user_id=user_id)
        if proposal is None:
            raise ValueError("proposal_not_found")
        if proposal.status != "locked_for_implementation":
            raise ValueError("proposal_must_be_locked_for_implementation")
        if proposal.current_version != int(proposal_version):
            raise ValueError("stale_proposal_version")
        if (
            proposal.account_id != account_id
            or proposal.conversation_id != conversation_id
        ):
            raise PermissionError("proposal_scope_mismatch")

        versions = self.proposals.list_versions(
            proposal_id,
            user_id=user_id,
        )
        version = next(
            (item for item in versions if item.version == int(proposal_version)),
            None,
        )
        if version is None:
            raise ValueError("proposal_version_not_found")
        spec = self.compiler.compile(
            version.proposal_json,
            base_strategy_id=version.base_strategy_id,
            base_strategy_version=version.base_strategy_version,
        )
        identity_hash = sha256_text(
            canonical_json(
                {
                    "proposal_id": proposal_id,
                    "proposal_version": int(proposal_version),
                    "user_id": user_id,
                    "spec": spec.to_dict(),
                }
            )
        )
        implementation_id = f"strategy_impl_{identity_hash[:24]}"
        target = (
            self.drafts_root
            / _safe_segment(user_id)
            / _safe_segment(proposal_id)
            / f"v{int(proposal_version)}"
        )
        target_parent = target.parent
        target_parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".v{int(proposal_version)}_",
                dir=target_parent,
            )
        )
        try:
            manifest, manifest_hash = self._write_artifacts(
                temporary,
                implementation_id=implementation_id,
                proposal=proposal.to_dict(),
                version=version.to_dict(),
                spec=spec,
                run_id=run_id,
            )
            if target.exists():
                shutil.rmtree(temporary)
            else:
                temporary.replace(target)
            manifest = json.loads(
                (target / "artifact_manifest.json").read_text(encoding="utf-8")
            )
            manifest_hash = sha256_text(
                (target / "artifact_manifest.json").read_text(encoding="utf-8")
            )
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise

        now = _now_text()
        record = {
            "implementation_id": implementation_id,
            "proposal_id": proposal_id,
            "proposal_version": int(proposal_version),
            "user_id": user_id,
            "account_id": account_id,
            "conversation_id": conversation_id,
            "implementation_type": spec.implementation_type,
            "artifact_root": str(target.resolve()),
            "implementation_hash": str(
                manifest.get("implementation_hash") or identity_hash
            ),
            "artifact_manifest_hash": manifest_hash,
            "status": "prepared",
            "created_at": now,
            "updated_at": now,
        }
        self.repository.upsert_implementation(record)
        self.proposals.set_status(
            proposal_id,
            user_id=user_id,
            status="implementation_ready",
            expected_version=proposal_version,
        )
        return StrategyImplementation(
            **record,
            artifact_manifest=manifest,
        )

    def get(
        self,
        implementation_id: str,
        *,
        user_id: str,
    ) -> StrategyImplementation | None:
        row = self.repository.get_implementation(
            implementation_id,
            user_id=user_id,
        )
        return self._from_record(row) if row else None

    def _from_record(
        self,
        record: dict[str, Any],
    ) -> StrategyImplementation:
        root = Path(str(record.get("artifact_root") or ""))
        manifest_path = root / "artifact_manifest.json"
        manifest = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else {}
        )
        return StrategyImplementation(
            implementation_id=str(record.get("implementation_id") or ""),
            proposal_id=str(record.get("proposal_id") or ""),
            proposal_version=int(record.get("proposal_version") or 0),
            user_id=str(record.get("user_id") or ""),
            account_id=str(record.get("account_id") or ""),
            conversation_id=str(record.get("conversation_id") or ""),
            implementation_type=str(record.get("implementation_type") or ""),
            artifact_root=str(record.get("artifact_root") or ""),
            implementation_hash=str(record.get("implementation_hash") or ""),
            artifact_manifest_hash=str(
                record.get("artifact_manifest_hash") or ""
            ),
            status=str(record.get("status") or ""),
            created_at=str(record.get("created_at") or ""),
            updated_at=str(record.get("updated_at") or ""),
            artifact_manifest=manifest,
        )

    def _write_artifacts(
        self,
        root: Path,
        *,
        implementation_id: str,
        proposal: dict[str, Any],
        version: dict[str, Any],
        spec: Any,
        run_id: str,
    ) -> tuple[dict[str, Any], str]:
        root.mkdir(parents=True, exist_ok=True)
        (root / "generated_code").mkdir(parents=True, exist_ok=True)
        payloads: dict[str, Any] = {
            "proposal_snapshot.json": {
                "proposal": proposal,
                "locked_version": version,
            },
            "implementation_spec.json": spec.to_dict(),
            "generated_config.json": spec.generated_config,
            "security_report.json": {"status": "pending_phase_3"},
            "test_report.json": {"status": "pending_phase_3"},
            "backtest_report.json": {"status": "pending_phase_3"},
        }
        for name, payload in payloads.items():
            (root / name).write_text(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        generated = self.generator.generate(
            spec,
            artifact_root=root,
            allowed_root=root,
        )
        security = self.validator.scan_generated_code(root / "generated_code")
        (root / "security_report.json").write_text(
            json.dumps(
                security,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        if security["status"] != "passed":
            raise ValueError("generated_code_failed_prepare_security_scan")

        (root / "diff.patch").write_text(
            "\n".join(
                [
                    "# Isolated strategy draft; no formal file has been changed.",
                    f"# implementation_type={spec.implementation_type}",
                    *[
                        f"# planned_add={path}"
                        for path in spec.formal_files
                    ],
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "implementation_preview.md").write_text(
            "\n".join(
                [
                    "# 隔离实施预览",
                    "",
                    f"- 实现 ID：`{implementation_id}`",
                    f"- Proposal：`{proposal['proposal_id']}` v{version['version']}",
                    f"- 路径：`{spec.implementation_type}`",
                    "- 当前只生成隔离草稿，未修改正式项目、Registry、Binding 或持仓。",
                    "- 安全、测试和回测将在 Phase 3 完整执行。",
                    "",
                    "本项目仅用于机器学习、金融数据分析和项目展示，不构成投资建议，不用于实盘交易。",
                ]
            ),
            encoding="utf-8",
        )
        files = sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and path.name != "artifact_manifest.json"
        )
        artifact_hashes = {
            path.relative_to(root).as_posix(): sha256_file(path)
            for path in files
        }
        implementation_hash = sha256_text(canonical_json(artifact_hashes))
        manifest = {
            "implementation_id": implementation_id,
            "proposal_id": proposal["proposal_id"],
            "proposal_version": int(version["version"]),
            "implementation_type": spec.implementation_type,
            "implementation_hash": implementation_hash,
            "artifact_hashes": artifact_hashes,
            "generated_files": [
                path.relative_to(root).as_posix() for path in generated
            ],
            "formal_files_planned": list(spec.formal_files),
            "source_run_id": str(run_id or ""),
        }
        manifest_text = json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        (root / "artifact_manifest.json").write_text(
            manifest_text,
            encoding="utf-8",
        )
        return manifest, sha256_text(manifest_text)
