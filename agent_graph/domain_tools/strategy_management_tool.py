from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_graph.session.confirmation_manager import (
    create_confirmation_plan,
    mark_plan_executed,
    validate_confirmation,
)
from agent_graph.domain_tools.audit_tool import write_agent_action_log, write_agent_confirmation_log
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult
from strategies.registry import StrategyManifest, get_strategy_registry


def _legacy_direct_write_disabled(*args: Any, **kwargs: Any) -> None:
    raise RuntimeError("legacy strategy direct write is disabled; use Write Gateway")


def manage_strategy(
    user_id: str,
    action: str = "list",
    strategy_id: str = "",
    version: str = "",
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    session_id: str = "",
) -> ToolResult:
    registry = get_strategy_registry(output_dir=output_dir, db_path=db_path)
    action_value = str(action or "list").strip().lower()

    if action_value == "list":
        return ToolResult(
            success=True,
            message="策略列表已读取。",
            data={
                "operation_type": "strategy_change",
                "strategies": [item.to_dict() for item in registry.list()],
            },
            permission=ToolPermission.READ,
            tool_name="strategy_management_tool",
        )

    if action_value in {"enable", "switch"}:
        manifest = registry.get(strategy_id, version)
        if manifest is None:
            return ToolResult(
                success=False,
                message="未找到要启用的策略版本。",
                data={"strategy_id": strategy_id, "version": version},
                errors=["strategy_version_not_found"],
                permission=ToolPermission.PREVIEW,
                tool_name="strategy_management_tool",
            )
        payload = {
            "operation_type": "strategy_change",
            "strategy_id": manifest.strategy_id,
            "strategy_version": manifest.version,
            "before_state_summary": {
                "enabled_strategies": [
                    item.to_dict()
                    for item in registry.list()
                    if item.enabled_for_paper_trading
                ]
            },
            "proposed_changes": [
                {
                    "type": "enable_strategy_version",
                    "strategy_id": manifest.strategy_id,
                    "version": manifest.version,
                }
            ],
            "after_state_preview": {
                "enabled_strategy_id": manifest.strategy_id,
                "enabled_version": manifest.version,
                "paper_orders_executed_now": False,
            },
            "strategy_manifest": manifest.to_dict(),
            "warnings": [
                "启用策略只影响后续目标持仓生成，不会立即执行今天的订单。"
            ],
        }
        plan = create_confirmation_plan(
            user_id,
            "enable_strategy",
            payload,
            output_dir=output_dir,
            db_path=db_path,
        )
        write_agent_confirmation_log(
            user_id,
            plan_id=str(plan["plan_id"]),
            confirmation_status="pending",
            expires_at=str(plan.get("expires_at") or ""),
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        data = dict(payload)
        data.update(
            {
                "plan_id": plan["plan_id"],
                "confirmation_token": plan["confirmation_token"],
                "expires_at": plan["expires_at"],
            }
        )
        return ToolResult(
            success=True,
            message="已生成策略启用确认计划。",
            data=data,
            warnings=list(payload["warnings"]),
            permission=ToolPermission.PREVIEW,
            tool_name="strategy_management_tool",
            requires_confirmation=True,
            confirmation_token=str(plan["confirmation_token"]),
        )

    if action_value == "disable":
        from agent_graph.services.write_operation_service import write_operation_service

        return write_operation_service.create_strategy_disable_proposal(
            user_id,
            strategy_id,
            version,
            output_dir=output_dir,
            db_path=db_path,
            session_id=session_id,
        )
        try:
            manifest = _legacy_direct_write_disabled(strategy_id, version)
        except ValueError as exc:
            return ToolResult(
                success=False,
                message=str(exc),
                data={"strategy_id": strategy_id, "version": version},
                errors=[str(exc)],
                permission=ToolPermission.WRITE,
                tool_name="strategy_management_tool",
            )
        write_agent_action_log(
            user_id,
            intent="strategy_change",
            tool_name="strategy_management_tool",
            tool_input={"action": action_value, "strategy_id": strategy_id, "version": version},
            tool_output_summary=manifest.to_dict(),
            execution_status="executed",
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        return ToolResult(
            success=True,
            message="策略已停用。",
            data={"strategy_manifest": manifest.to_dict()},
            permission=ToolPermission.WRITE,
            tool_name="strategy_management_tool",
        )

    return ToolResult(
        success=False,
        message="不支持的策略管理动作。",
        data={"action": action_value},
        errors=["unsupported_strategy_management_action"],
        permission=ToolPermission.READ,
        tool_name="strategy_management_tool",
    )


def execute_confirmed_strategy_plan(
    user_id: str,
    plan_id: str,
    confirmation_token: str,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    session_id: str = "",
) -> ToolResult:
    ok, status, plan = validate_confirmation(
        user_id,
        plan_id,
        confirmation_token,
        output_dir=output_dir,
        db_path=db_path,
    )
    if not ok or not plan:
        write_agent_confirmation_log(
            user_id,
            plan_id=plan_id,
            confirmation_status=status,
            session_id=session_id,
            error_message=status,
            output_dir=output_dir,
            db_path=db_path,
        )
        return ToolResult(
            success=False,
            message=f"策略确认被拒绝：{status}",
            data={"plan_id": plan_id, "confirmation_status": status},
            errors=[status],
            permission=ToolPermission.WRITE,
            tool_name="strategy_management_tool",
        )

    registry = get_strategy_registry(output_dir=output_dir, db_path=db_path)
    intent = str(plan.get("intent") or "")

    if intent == "register_strategy":
        raw_manifest = dict(plan.get("strategy_manifest") or {})
        manifest = StrategyManifest(**raw_manifest)
        registered = registry.register(manifest)
        enable_result = manage_strategy(
            user_id,
            action="enable",
            strategy_id=registered.strategy_id,
            version=registered.version,
            output_dir=output_dir,
            db_path=db_path,
            session_id=session_id,
        )
        mark_plan_executed(
            user_id,
            plan_id,
            output_dir=output_dir,
            db_path=db_path,
            strategy_status="registered",
        )
        write_agent_confirmation_log(
            user_id,
            plan_id=plan_id,
            confirmation_status="confirmed",
            expires_at=str(plan.get("expires_at") or ""),
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        write_agent_action_log(
            user_id,
            intent="register_strategy",
            tool_name="strategy_management_tool",
            tool_input={"plan_id": plan_id},
            tool_output_summary={
                "strategy_id": registered.strategy_id,
                "version": registered.version,
                "enable_plan_created": bool((enable_result.data or {}).get("plan_id")),
            },
            plan_id=plan_id,
            confirmation_status="confirmed",
            execution_status="registered",
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        return ToolResult(
            success=True,
            message="策略版本已注册为未启用状态；已生成单独的启用确认计划。",
            data={
                "plan_id": plan_id,
                "strategy_manifest": registered.to_dict(),
                "enable_plan": enable_result.data,
            },
            warnings=list(enable_result.warnings or []),
            permission=ToolPermission.WRITE,
            tool_name="strategy_management_tool",
        )

    if intent == "enable_strategy":
        strategy_id = str(plan.get("strategy_id") or "")
        version = str(plan.get("strategy_version") or "")
        enabled = registry.enable(strategy_id, version)
        mark_plan_executed(
            user_id,
            plan_id,
            output_dir=output_dir,
            db_path=db_path,
            strategy_status="enabled",
        )
        write_agent_confirmation_log(
            user_id,
            plan_id=plan_id,
            confirmation_status="confirmed",
            expires_at=str(plan.get("expires_at") or ""),
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        write_agent_action_log(
            user_id,
            intent="enable_strategy",
            tool_name="strategy_management_tool",
            tool_input={"plan_id": plan_id},
            tool_output_summary=enabled.to_dict(),
            plan_id=plan_id,
            confirmation_status="confirmed",
            execution_status="enabled",
            session_id=session_id,
            output_dir=output_dir,
            db_path=db_path,
        )
        return ToolResult(
            success=True,
            message="策略版本已启用；不会立即执行今天的模拟盘订单。",
            data={"strategy_manifest": enabled.to_dict()},
            permission=ToolPermission.WRITE,
            tool_name="strategy_management_tool",
        )

    return ToolResult(
        success=False,
        message="该确认计划不是策略管理计划。",
        data={"plan_id": plan_id, "intent": intent},
        errors=["unsupported_strategy_plan_intent"],
        permission=ToolPermission.WRITE,
        tool_name="strategy_management_tool",
    )
