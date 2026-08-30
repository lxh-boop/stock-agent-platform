from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any

from agent.services.strategy_implementation_service import (
    StrategyImplementation,
    StrategyImplementationService,
    canonical_json,
    sha256_file,
    sha256_text,
)
from agent.session.confirmation_manager import (
    create_confirmation_plan,
    inspect_confirmation,
    mark_plan_executed,
    mark_plan_revalidation_failed,
    persist_action_commit,
    validate_confirmation,
)
from agent.tools.audit_tool import (
    write_agent_action_log,
    write_agent_confirmation_log,
)
from agent.tools.tool_schemas import ToolPermission, ToolResult
from database.connection import get_connection
from strategies.registry import (
    StrategyManifest,
    get_strategy_registry,
)


def _now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _safe_segment(value: str) -> str:
    result = "".join(
        character
        for character in str(value or "")
        if character.isalnum() or character in {"_", "-", "."}
    )
    if not result or result in {".", ".."}:
        raise ValueError("invalid_formal_strategy_path_segment")
    return result


class StrategyApplyService:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        output_dir: str | Path = "outputs",
        runtime_dir: str | Path = "runtime",
        project_root: str | Path = ".",
    ) -> None:
        self.db_path = db_path
        self.output_dir = Path(output_dir)
        self.runtime_dir = Path(runtime_dir)
        self.project_root = Path(project_root).resolve()
        self.implementations = StrategyImplementationService(
            db_path=db_path,
            runtime_dir=runtime_dir,
        )

    def create_plan(
        self,
        *,
        implementation_id: str,
        user_id: str,
        account_id: str,
        conversation_id: str,
        run_id: str,
    ) -> ToolResult:
        implementation = self.implementations.get(
            implementation_id,
            user_id=user_id,
        )
        if implementation is None:
            return self._failure("implementation_not_found")
        if implementation.status != "validated":
            return self._failure(
                "implementation_must_be_validated",
                data={"implementation_id": implementation_id},
            )
        if (
            implementation.account_id != account_id
            or implementation.conversation_id != conversation_id
        ):
            return self._failure("implementation_scope_mismatch")
        root = Path(implementation.artifact_root)
        manifest_text = (root / "artifact_manifest.json").read_text(
            encoding="utf-8"
        )
        manifest = json.loads(manifest_text)
        strategy_id, strategy_version = self._strategy_identity(
            implementation
        )
        formal_target = self._formal_target(
            implementation,
            strategy_id,
            strategy_version,
        )
        baseline_hashes = dict(
            manifest.get("formal_baseline_hashes") or {}
        )
        payload = {
            "operation_type": "apply_strategy_implementation",
            "proposal_id": implementation.proposal_id,
            "proposal_version": implementation.proposal_version,
            "implementation_id": implementation.implementation_id,
            "implementation_hash": implementation.implementation_hash,
            "artifact_manifest_hash": sha256_text(manifest_text),
            "diff_hash": str(manifest.get("diff_hash") or ""),
            "security_report_hash": str(
                manifest.get("security_report_hash") or ""
            ),
            "test_report_hash": str(
                manifest.get("test_report_hash") or ""
            ),
            "backtest_report_hash": str(
                manifest.get("backtest_report_hash") or ""
            ),
            "baseline_code_hash": sha256_text(
                canonical_json(baseline_hashes)
            ),
            "baseline_strategy_hash": sha256_file(
                root / "generated_config.json"
            ),
            "user_id": user_id,
            "account_id": account_id,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "formal_target": str(formal_target),
            "before_state_summary": {
                "implementation_status": implementation.status,
                "proposal_version": implementation.proposal_version,
                "formal_baseline_hashes": baseline_hashes,
                "registry_has_version": False,
            },
            "proposed_changes": [
                {
                    "type": "apply_strategy_implementation",
                    "implementation_type": implementation.implementation_type,
                    "formal_target": str(formal_target),
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                }
            ],
            "after_state_preview": {
                "strategy_status": "registered_disabled",
                "enabled_for_paper_trading": False,
                "binding_changed": False,
                "positions_changed": False,
            },
            "validation_results": {
                "security": "passed",
                "tests": "passed",
                "backtest": "passed",
                "artifact_hash_bound": True,
            },
            "warnings": [
                "确认后只注册禁用策略版本；不会启用策略或修改当前持仓。"
            ],
        }
        plan = create_confirmation_plan(
            user_id,
            "apply_strategy_implementation",
            payload,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        write_agent_confirmation_log(
            user_id,
            plan_id=str(plan["plan_id"]),
            confirmation_status="pending",
            expires_at=str(plan.get("expires_at") or ""),
            session_id=conversation_id,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        return ToolResult(
            success=True,
            message="已生成策略应用与注册确认计划。",
            data={
                **payload,
                "plan_id": plan["plan_id"],
                "confirmation_token": plan["confirmation_token"],
                "plan_hash": plan["plan_hash"],
                "expires_at": plan["expires_at"],
            },
            warnings=list(payload["warnings"]),
            permission=ToolPermission.PREVIEW,
            tool_name="strategy.create_apply_plan",
            requires_confirmation=True,
            confirmation_token=str(plan["confirmation_token"]),
        )

    def commit(
        self,
        *,
        user_id: str,
        plan_id: str,
        confirmation_token: str,
        conversation_id: str = "",
    ) -> ToolResult:
        ok, status, plan = inspect_confirmation(
            user_id,
            plan_id,
            confirmation_token,
            output_dir=self.output_dir,
            db_path=self.db_path,
            record_failure=True,
        )
        if not ok or not plan:
            return self._failure(
                status,
                data={"plan_id": plan_id},
                permission=ToolPermission.WRITE,
            )
        if str(plan.get("intent") or "") != "apply_strategy_implementation":
            return self._failure(
                "unsupported_plan_intent",
                data={"plan_id": plan_id},
                permission=ToolPermission.WRITE,
            )
        target_hint = Path(str(plan.get("formal_target") or "")).resolve()
        for parent in target_hint.parents:
            if parent.name == "strategies":
                self.project_root = parent.parent.resolve()
                break
        revalidation_error, implementation = self._revalidate(plan)
        if revalidation_error or implementation is None:
            mark_plan_revalidation_failed(
                user_id,
                plan_id,
                reason=revalidation_error,
                output_dir=self.output_dir,
                db_path=self.db_path,
            )
            persist_action_commit(
                plan,
                db_path=self.db_path,
                status="rejected",
                error_type=revalidation_error,
                error_message=revalidation_error,
            )
            return self._failure(
                revalidation_error,
                data={"plan_id": plan_id},
                permission=ToolPermission.WRITE,
            )
        confirmed, confirm_status, confirmed_plan = validate_confirmation(
            user_id,
            plan_id,
            confirmation_token,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        if not confirmed or confirmed_plan is None:
            return self._failure(
                confirm_status,
                data={"plan_id": plan_id},
                permission=ToolPermission.WRITE,
            )

        registry = get_strategy_registry(
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        registry_path = registry.registry_path
        registry_existed = registry_path.exists()
        registry_before = (
            registry_path.read_bytes() if registry_existed else b""
        )
        target = Path(str(plan.get("formal_target") or "")).resolve()
        created_root: Path | None = None
        try:
            created_root, source_file = self._apply_formal_artifact(
                implementation,
                target=target,
            )
            strategy_id = str(plan.get("strategy_id") or "")
            version = str(plan.get("strategy_version") or "")
            manifest = self._build_manifest(
                implementation,
                source_file=source_file,
                strategy_id=strategy_id,
                version=version,
                plan_id=plan_id,
            )
            registered = registry.register(manifest)
            self.implementations.repository.update_implementation(
                implementation.implementation_id,
                {
                    "status": "registered",
                    "updated_at": _now_text(),
                },
            )
        except Exception as exc:
            if created_root and created_root.exists():
                shutil.rmtree(created_root, ignore_errors=True)
            self._restore_registry(
                registry_path,
                existed=registry_existed,
                content=registry_before,
                strategy_id=str(plan.get("strategy_id") or ""),
                version=str(plan.get("strategy_version") or ""),
            )
            mark_plan_revalidation_failed(
                user_id,
                plan_id,
                reason=f"apply_failed:{type(exc).__name__}",
                output_dir=self.output_dir,
                db_path=self.db_path,
            )
            persist_action_commit(
                confirmed_plan,
                db_path=self.db_path,
                status="rejected",
                error_type="apply_failed",
                error_message=f"{type(exc).__name__}:{exc}",
            )
            return self._failure(
                "apply_failed",
                data={"plan_id": plan_id},
                permission=ToolPermission.WRITE,
            )

        mark_plan_executed(
            user_id,
            plan_id,
            output_dir=self.output_dir,
            db_path=self.db_path,
            strategy_status="registered_disabled",
            strategy_id=registered.strategy_id,
            strategy_version=registered.version,
        )
        write_agent_confirmation_log(
            user_id,
            plan_id=plan_id,
            confirmation_status="confirmed",
            expires_at=str(plan.get("expires_at") or ""),
            session_id=conversation_id,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        write_agent_action_log(
            user_id,
            intent="apply_strategy_implementation",
            tool_name="strategy.apply.commit",
            tool_input={
                "plan_id": plan_id,
                "implementation_id": implementation.implementation_id,
            },
            tool_output_summary={
                "strategy_id": registered.strategy_id,
                "strategy_version": registered.version,
                "status": registered.status,
                "enabled_for_paper_trading": False,
                "formal_target": str(target),
            },
            plan_id=plan_id,
            confirmation_status="confirmed",
            execution_status="executed",
            session_id=conversation_id,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        from agent.services.strategy_binding_service import (
            StrategyBindingService,
        )

        activation_plan = StrategyBindingService(
            db_path=self.db_path,
            output_dir=self.output_dir,
        ).create_activation_plan(
            user_id=user_id,
            account_id=str(plan.get("account_id") or ""),
            strategy_id=registered.strategy_id,
            strategy_version=registered.version,
            effective_from=(
                StrategyBindingService.default_effective_from()
            ),
            conversation_id=conversation_id,
            run_id=str(plan.get("run_id") or ""),
        )
        return ToolResult(
            success=True,
            message="策略实现已应用并注册为禁用版本。",
            data={
                "plan_id": plan_id,
                "commit_id": f"commit_{plan_id}",
                "implementation_id": implementation.implementation_id,
                "strategy_manifest": registered.to_dict(),
                "formal_target": str(target),
                "activation_plan": (
                    activation_plan.data
                    if activation_plan.success
                    else {}
                ),
                "binding_changed": False,
                "positions_changed": False,
            },
            permission=ToolPermission.WRITE,
            tool_name="strategy.apply.commit",
        )

    def _revalidate(
        self,
        plan: dict[str, Any],
    ) -> tuple[str, StrategyImplementation | None]:
        user_id = str(plan.get("user_id") or "")
        implementation = self.implementations.get(
            str(plan.get("implementation_id") or ""),
            user_id=user_id,
        )
        if implementation is None:
            return "implementation_not_found", None
        if implementation.status != "validated":
            return "implementation_status_changed", None
        if (
            implementation.proposal_id != str(plan.get("proposal_id") or "")
            or implementation.proposal_version
            != int(plan.get("proposal_version") or 0)
            or implementation.account_id
            != str(plan.get("account_id") or "")
            or implementation.conversation_id
            != str(plan.get("conversation_id") or "")
        ):
            return "implementation_scope_or_proposal_changed", None
        proposal = self.implementations.proposals.get(
            implementation.proposal_id,
            user_id=user_id,
        )
        if (
            proposal is None
            or proposal.current_version != implementation.proposal_version
            or proposal.status != "implementation_ready"
        ):
            return "proposal_version_or_status_changed", None
        root = Path(implementation.artifact_root)
        manifest_text = (root / "artifact_manifest.json").read_text(
            encoding="utf-8"
        )
        manifest = json.loads(manifest_text)
        artifact_hashes = dict(manifest.get("artifact_hashes") or {})
        current_artifact_hashes: dict[str, str] = {}
        for relative, expected_hash in artifact_hashes.items():
            path = root / relative
            if not path.exists():
                return f"artifact_missing:{relative}", None
            current_hash = sha256_file(path)
            current_artifact_hashes[relative] = current_hash
            if current_hash != str(expected_hash or ""):
                if relative == "diff.patch":
                    return "diff_hash_changed", None
                if relative == "security_report.json":
                    return "security_report_hash_changed", None
                if relative == "test_report.json":
                    return "test_report_hash_changed", None
                if relative == "backtest_report.json":
                    return "backtest_report_hash_changed", None
                return f"artifact_changed:{relative}", None
        current_implementation_hash = sha256_text(
            canonical_json(current_artifact_hashes)
        )
        checks = {
            "implementation_hash": current_implementation_hash,
            "artifact_manifest_hash": sha256_text(manifest_text),
            "diff_hash": current_artifact_hashes.get("diff.patch", ""),
            "security_report_hash": current_artifact_hashes.get(
                "security_report.json",
                "",
            ),
            "test_report_hash": current_artifact_hashes.get(
                "test_report.json",
                "",
            ),
            "backtest_report_hash": current_artifact_hashes.get(
                "backtest_report.json",
                "",
            ),
            "baseline_strategy_hash": sha256_file(
                root / "generated_config.json"
            ),
        }
        for key, current in checks.items():
            if str(plan.get(key) or "") != str(current or ""):
                return f"{key}_changed", None
        current_baseline = self._current_baseline_hash(
            dict(manifest.get("formal_baseline_hashes") or {})
        )
        if str(plan.get("baseline_code_hash") or "") != current_baseline:
            return "baseline_code_hash_changed", None
        registry = get_strategy_registry(
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        if registry.get(
            str(plan.get("strategy_id") or ""),
            str(plan.get("strategy_version") or ""),
        ):
            return "strategy_version_already_registered", None
        return "", implementation

    def _current_baseline_hash(
        self,
        expected: dict[str, str],
    ) -> str:
        current: dict[str, str] = {}
        for relative in expected:
            path = self.project_root / relative
            if not path.exists():
                current[relative] = ""
            else:
                current[relative] = sha256_file(path)
        return sha256_text(canonical_json(current))

    @staticmethod
    def _strategy_identity(
        implementation: StrategyImplementation,
    ) -> tuple[str, str]:
        strategy_id = (
            f"user_strategy_{implementation.implementation_hash[:16]}"
        )
        version = (
            f"v{implementation.proposal_version}_"
            f"{implementation.implementation_hash[16:28]}"
        )
        return strategy_id, version

    def _formal_target(
        self,
        implementation: StrategyImplementation,
        strategy_id: str,
        version: str,
    ) -> Path:
        root = self.project_root / "strategies"
        if implementation.implementation_type == "config":
            path = (
                root
                / "config_versions"
                / _safe_segment(strategy_id)
                / f"{_safe_segment(version)}.json"
            )
        elif implementation.implementation_type == "composite":
            path = (
                root
                / "generated"
                / f"{_safe_segment(strategy_id)}_{_safe_segment(version)}"
                / "composition.json"
            )
        else:
            path = (
                root
                / "generated"
                / f"{_safe_segment(strategy_id)}_{_safe_segment(version)}"
                / "strategy_plugin.py"
            )
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise PermissionError("formal_strategy_target_outside_strategies")
        return resolved

    def _apply_formal_artifact(
        self,
        implementation: StrategyImplementation,
        *,
        target: Path,
    ) -> tuple[Path, Path]:
        strategy_root = (self.project_root / "strategies").resolve()
        if not target.is_relative_to(strategy_root):
            raise PermissionError("formal_strategy_target_outside_strategies")
        if target.exists():
            raise FileExistsError("formal_strategy_version_already_exists")
        artifact_root = Path(implementation.artifact_root)
        if implementation.implementation_type == "config":
            source = artifact_root / "generated_config.json"
        elif implementation.implementation_type == "composite":
            source = artifact_root / "generated_code" / "composition.json"
        else:
            source = artifact_root / "generated_code" / "strategy_plugin.py"
        created_root = target.parent
        created_root.mkdir(parents=True, exist_ok=False)
        shutil.copy2(source, target)
        if implementation.implementation_type == "code":
            (created_root / "__init__.py").write_text("", encoding="utf-8")
        return created_root, target

    def _build_manifest(
        self,
        implementation: StrategyImplementation,
        *,
        source_file: Path,
        strategy_id: str,
        version: str,
        plan_id: str,
    ) -> StrategyManifest:
        config = json.loads(
            (
                Path(implementation.artifact_root)
                / "generated_config.json"
            ).read_text(encoding="utf-8")
        )
        if implementation.implementation_type == "code":
            relative = source_file.relative_to(self.project_root)
            module_path = ".".join(relative.with_suffix("").parts)
            class_name = "GeneratedIsolatedStrategy"
            source_type = "generated_plugin"
        else:
            module_path = (
                "strategies.adapters.hierarchical_top10_strategy"
            )
            class_name = "HierarchicalTop10Strategy"
            source_type = (
                "config_version"
                if implementation.implementation_type == "config"
                else "composite_version"
            )
        return StrategyManifest(
            strategy_id=strategy_id,
            strategy_name=f"User strategy {strategy_id[-8:]}",
            version=version,
            source_type=source_type,
            module_path=module_path,
            class_name=class_name,
            config_schema={
                "type": "object",
                "properties": {
                    key: {"type": "number"}
                    for key in config
                },
                "additionalProperties": False,
            },
            status="registered_disabled",
            created_by=implementation.user_id,
            code_hash=sha256_file(source_file),
            validation_status="passed",
            backtest_status="passed",
            enabled_for_paper_trading=False,
            metadata={
                "proposal_id": implementation.proposal_id,
                "proposal_version": implementation.proposal_version,
                "implementation_id": implementation.implementation_id,
                "implementation_hash": implementation.implementation_hash,
                "source_plan_id": plan_id,
                "config": config,
                "formal_file": str(source_file),
            },
        )

    def _restore_registry(
        self,
        path: Path,
        *,
        existed: bool,
        content: bytes,
        strategy_id: str,
        version: str,
    ) -> None:
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        elif path.exists():
            path.unlink()
        if self.db_path is not None:
            try:
                with get_connection() as connection:
                    connection.execute(
                        "DELETE FROM strategy_registry "
                        "WHERE strategy_id=%s AND version=%s",
                        (strategy_id, version),
                    )
                    connection.commit()
            except Exception:
                pass

    @staticmethod
    def _failure(
        error: str,
        *,
        data: dict[str, Any] | None = None,
        permission: str = ToolPermission.PREVIEW,
    ) -> ToolResult:
        return ToolResult(
            success=False,
            message=f"策略应用操作失败：{error}",
            data=dict(data or {}),
            errors=[error],
            permission=permission,
            tool_name=(
                "strategy.apply.commit"
                if permission == ToolPermission.WRITE
                else "strategy.create_apply_plan"
            ),
        )
