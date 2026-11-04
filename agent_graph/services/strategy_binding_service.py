from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

from agent_graph.services.strategy_config_compiler import StrategyConfigCompiler
from agent_graph.session.confirmation_manager import (
    create_confirmation_plan,
    inspect_confirmation,
    mark_plan_executed,
    mark_plan_revalidation_failed,
    persist_action_commit,
    validate_confirmation,
)
from agent_graph.domain_tools.audit_tool import (
    write_agent_action_log,
    write_agent_confirmation_log,
)
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult
from strategies.binding_repository import (
    StrategyBinding,
    StrategyBindingRepository,
)
from strategies.registry import get_strategy_registry


def _config_hash(config: dict[str, Any]) -> str:
    canonical = StrategyConfigCompiler._canonical_config(config)
    return hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class StrategyBindingService:
    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        output_dir: str | Path = "outputs",
    ) -> None:
        self.db_path = db_path
        self.output_dir = Path(output_dir)
        self.bindings = StrategyBindingRepository(db_path)
        self.registry = get_strategy_registry(
            output_dir=output_dir,
            db_path=db_path,
        )

    @staticmethod
    def default_effective_from() -> str:
        return (datetime.now(UTC).date() + timedelta(days=1)).isoformat()

    def create_activation_plan(
        self,
        *,
        user_id: str,
        account_id: str,
        strategy_id: str,
        strategy_version: str,
        effective_from: str = "",
        conversation_id: str = "",
        run_id: str = "",
        rollback_target_binding_id: str = "",
    ) -> ToolResult:
        manifest = self.registry.get(strategy_id, strategy_version)
        if manifest is None:
            return self._failure("strategy_version_not_found")
        if manifest.enabled_for_paper_trading:
            return self._failure(
                "binding_requires_registered_disabled_version"
            )
        try:
            effective = date.fromisoformat(
                effective_from or self.default_effective_from()
            ).isoformat()
        except ValueError:
            return self._failure("invalid_effective_from")
        config = dict(manifest.metadata.get("config") or {})
        config_hash = _config_hash(config)
        current = self.bindings.get_effective(
            user_id=user_id,
            account_id=account_id,
        )
        intent = (
            "rollback_strategy_binding"
            if rollback_target_binding_id
            else "activate_strategy_binding"
        )
        payload = {
            "operation_type": intent,
            "user_id": user_id,
            "account_id": account_id,
            "strategy_id": manifest.strategy_id,
            "strategy_version": manifest.version,
            "config_hash": config_hash,
            "effective_from": effective,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "rollback_target_binding_id": rollback_target_binding_id,
            "before_state_summary": {
                "current_binding": current.to_dict() if current else {},
            },
            "proposed_changes": [
                {
                    "type": intent,
                    "strategy_id": manifest.strategy_id,
                    "strategy_version": manifest.version,
                    "effective_from": effective,
                }
            ],
            "after_state_preview": {
                "new_strategy_id": manifest.strategy_id,
                "new_strategy_version": manifest.version,
                "effective_from": effective,
                "affects_today": effective
                <= datetime.now(UTC).date().isoformat(),
                "changes_current_positions": False,
                "rollback_target": (
                    current.binding_id if current else ""
                ),
            },
            "validation_results": {
                "strategy_registered": True,
                "strategy_disabled_globally": True,
                "config_hash": config_hash,
                "account_scope": account_id,
            },
            "warnings": [
                "绑定只影响生效日及之后的策略解析，不会修改当前持仓。"
            ],
        }
        plan = create_confirmation_plan(
            user_id,
            intent,
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
            message=(
                "已生成策略回滚绑定确认计划。"
                if rollback_target_binding_id
                else "已生成未来策略启用绑定确认计划。"
            ),
            data={
                **payload,
                "plan_id": plan["plan_id"],
                "confirmation_token": plan["confirmation_token"],
                "plan_hash": plan["plan_hash"],
                "expires_at": plan["expires_at"],
            },
            warnings=list(payload["warnings"]),
            permission=ToolPermission.PREVIEW,
            tool_name="strategy.create_activation_plan",
            requires_confirmation=True,
            confirmation_token=str(plan["confirmation_token"]),
        )

    def create_rollback_plan(
        self,
        *,
        user_id: str,
        account_id: str,
        conversation_id: str = "",
        run_id: str = "",
    ) -> ToolResult:
        current = self.bindings.get_effective(
            user_id=user_id,
            account_id=account_id,
        )
        if current is None or not current.previous_binding_id:
            return self._failure("previous_binding_not_found")
        target = self.bindings.get(
            current.previous_binding_id,
            user_id=user_id,
        )
        if target is None or target.account_id != account_id:
            return self._failure("previous_binding_not_found")
        return self.create_activation_plan(
            user_id=user_id,
            account_id=account_id,
            strategy_id=target.strategy_id,
            strategy_version=target.strategy_version,
            effective_from=datetime.now(UTC).date().isoformat(),
            conversation_id=conversation_id,
            run_id=run_id,
            rollback_target_binding_id=target.binding_id,
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
            return self._failure(status, permission=ToolPermission.WRITE)
        intent = str(plan.get("intent") or "")
        if intent not in {
            "activate_strategy_binding",
            "rollback_strategy_binding",
        }:
            return self._failure(
                "unsupported_plan_intent",
                permission=ToolPermission.WRITE,
            )
        error = self._revalidate(plan)
        if error:
            mark_plan_revalidation_failed(
                user_id,
                plan_id,
                reason=error,
                output_dir=self.output_dir,
                db_path=self.db_path,
            )
            persist_action_commit(
                plan,
                db_path=self.db_path,
                status="rejected",
                error_type=error,
                error_message=error,
            )
            return self._failure(error, permission=ToolPermission.WRITE)
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
                permission=ToolPermission.WRITE,
            )
        binding = self.bindings.activate(
            user_id=user_id,
            account_id=str(plan.get("account_id") or ""),
            strategy_id=str(plan.get("strategy_id") or ""),
            strategy_version=str(plan.get("strategy_version") or ""),
            config_hash=str(plan.get("config_hash") or ""),
            effective_from=str(plan.get("effective_from") or ""),
            source_plan_id=plan_id,
        )
        mark_plan_executed(
            user_id,
            plan_id,
            output_dir=self.output_dir,
            db_path=self.db_path,
            binding_id=binding.binding_id,
            strategy_status=binding.status,
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
            intent=intent,
            tool_name="strategy.binding.commit",
            tool_input={"plan_id": plan_id},
            tool_output_summary=binding.to_dict(),
            plan_id=plan_id,
            confirmation_status="confirmed",
            execution_status="executed",
            session_id=conversation_id,
            output_dir=self.output_dir,
            db_path=self.db_path,
        )
        return ToolResult(
            success=True,
            message="账户策略绑定已确认；当前持仓保持不变。",
            data={
                "plan_id": plan_id,
                "commit_id": f"commit_{plan_id}",
                "binding": binding.to_dict(),
                "positions_changed": False,
                "orders_created": False,
            },
            permission=ToolPermission.WRITE,
            tool_name="strategy.binding.commit",
        )

    def _revalidate(self, plan: dict[str, Any]) -> str:
        account_id = str(plan.get("account_id") or "")
        current = self.bindings.get_effective(
            user_id=str(plan.get("user_id") or ""),
            account_id=account_id,
        )
        expected = dict(
            (plan.get("before_state_summary") or {}).get(
                "current_binding"
            )
            or {}
        )
        if str(expected.get("binding_id") or "") != str(
            current.binding_id if current else ""
        ):
            return "binding_state_changed"
        manifest = self.registry.get(
            str(plan.get("strategy_id") or ""),
            str(plan.get("strategy_version") or ""),
        )
        if manifest is None:
            return "strategy_version_not_found"
        if manifest.enabled_for_paper_trading:
            return "strategy_global_state_changed"
        config_hash = _config_hash(
            dict(manifest.metadata.get("config") or {})
        )
        if config_hash != str(plan.get("config_hash") or ""):
            return "binding_config_hash_changed"
        rollback_target_id = str(
            plan.get("rollback_target_binding_id") or ""
        )
        if rollback_target_id:
            target = self.bindings.get(
                rollback_target_id,
                user_id=str(plan.get("user_id") or ""),
            )
            if target is None or target.account_id != account_id:
                return "rollback_target_changed"
        return ""

    @staticmethod
    def _failure(
        error: str,
        *,
        permission: str = ToolPermission.PREVIEW,
    ) -> ToolResult:
        return ToolResult(
            success=False,
            message=f"策略绑定操作失败：{error}",
            errors=[error],
            permission=permission,
            tool_name=(
                "strategy.binding.commit"
                if permission == ToolPermission.WRITE
                else "strategy.create_activation_plan"
            ),
        )
