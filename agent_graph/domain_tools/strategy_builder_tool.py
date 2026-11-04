from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from agent_graph.session.confirmation_manager import create_confirmation_plan
from agent_graph.domain_tools.audit_tool import write_agent_action_log, write_agent_confirmation_log
from agent_graph.domain_tools._common import now_text, safe_int
from agent_graph.domain_tools.tool_schemas import ToolPermission, ToolResult
from strategies.adapters.hierarchical_top10_strategy import HierarchicalTop10Strategy
from strategies.registry import StrategyManifest, get_strategy_registry


def _top_n_from_requirement(requirement: str, parameters: dict[str, Any]) -> int | None:
    raw = parameters.get("top_k") or parameters.get("target_position_count")
    value = safe_int(raw)
    if value is not None:
        return max(1, min(value, 10))
    text = str(requirement or "")
    match = re.search(r"(?:top\s*|前)\s*(\d{1,2})", text, flags=re.IGNORECASE)
    if match:
        return max(1, min(int(match.group(1)), 10))
    chinese = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    match = re.search(r"前([一二三四五六七八九十])", text)
    if match:
        return chinese.get(match.group(1))
    return None


def _target_ratio_from_requirement(requirement: str) -> float | None:
    text = str(requirement or "")
    if "现金" in text:
        match = re.search(r"现金\D{0,8}(\d+(?:\.\d+)?)\s*%", text)
        if match:
            return max(0.0, min(1.0, 1.0 - float(match.group(1)) / 100.0))
    return None


def _is_vague_style_only(requirement: str) -> bool:
    text = str(requirement or "")
    has_style_word = any(token in text for token in ["稳健", "激进", "保守", "大胆"])
    has_rule = any(
        token in text
        for token in ["前", "top", "%", "均线", "每周", "每月", "只持有", "仓位", "现金"]
    )
    return has_style_word and not has_rule


def _strategy_version(requirement: str, config: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        (str(requirement) + repr(sorted(config.items()))).encode("utf-8")
    ).hexdigest()[:10]
    return f"config_{now_text().replace('-', '').replace(':', '').replace(' ', '_')}_{digest}"


def prepare_strategy_change(
    user_id: str,
    requirement: str,
    parameters: dict[str, Any] | None = None,
    output_dir: str | Path = "outputs",
    db_path: str | Path | None = None,
    session_id: str = "",
) -> ToolResult:
    params = dict(parameters or {})
    registry = get_strategy_registry(output_dir=output_dir, db_path=db_path)

    if _is_vague_style_only(requirement):
        strategies = [item.to_dict() for item in registry.list()]
        return ToolResult(
            success=False,
            message=(
                "当前描述只有长期风格倾向，缺少可执行的选股、仓位或调仓规则；"
                "请补充具体规则，或从已注册策略中选择。"
            ),
            data={
                "operation_type": "strategy_change",
                "registered_strategies": strategies,
                "need_clarification": True,
            },
            warnings=["未创建风格层或策略参数。"],
            errors=["insufficient_strategy_rule"],
            permission=ToolPermission.PREVIEW,
            tool_name="strategy_builder_tool",
        )

    top_n = _top_n_from_requirement(requirement, params) or 10
    target_ratio = _target_ratio_from_requirement(requirement)
    strategy = HierarchicalTop10Strategy()
    config = {
        "top_n": top_n,
        "target_ratio": target_ratio if target_ratio is not None else 0.80,
        "min_cash_ratio": 0.05,
    }
    validation_errors = strategy.validate_config(config)
    status = "validated" if not validation_errors else "validation_failed"
    version = _strategy_version(requirement, config)
    manifest = StrategyManifest(
        strategy_id=f"{strategy.strategy_id}_config",
        strategy_name=f"{strategy.strategy_name} config",
        version=version,
        source_type="existing_strategy_config",
        module_path="strategies.adapters.hierarchical_top10_strategy",
        class_name="HierarchicalTop10Strategy",
        config_schema=strategy.get_config_schema(),
        status="approved" if not validation_errors else "validation_failed",
        created_by=str(user_id or "default"),
        validation_status="passed" if not validation_errors else "failed",
        backtest_status="not_run_missing_historical_backtest_context",
        enabled_for_paper_trading=False,
        metadata={
            "operation_type": "strategy_change",
            "original_user_request": requirement,
            "structured_strategy_spec": {
                "base_strategy": strategy.strategy_id,
                "config": config,
            },
            "implementation_type": "existing_strategy_config",
        },
    )
    payload = {
        "operation_type": "strategy_change",
        "strategy_id": manifest.strategy_id,
        "strategy_version": manifest.version,
        "original_user_request": requirement,
        "structured_strategy_spec": manifest.metadata["structured_strategy_spec"],
        "implementation_type": "existing_strategy_config",
        "strategy_manifest": manifest.to_dict(),
        "validation_result": {
            "status": status,
            "errors": validation_errors,
        },
        "security_scan_result": {
            "status": "not_required_existing_strategy_config",
            "errors": [],
        },
        "test_result": {
            "status": "interface_schema_validated" if not validation_errors else "failed",
            "errors": validation_errors,
        },
        "backtest_result": {
            "status": "not_run_missing_historical_backtest_context",
            "metrics": {},
        },
        "before_state_summary": {
            "enabled_strategies": [
                item.to_dict()
                for item in registry.list()
                if item.enabled_for_paper_trading
            ],
        },
        "proposed_changes": [
            {
                "type": "register_strategy_version",
                "strategy_id": manifest.strategy_id,
                "version": manifest.version,
                "enabled_after_registration": False,
            }
        ],
        "after_state_preview": {
            "status_after_registration": "approved_disabled",
            "enable_requires_second_confirmation": True,
        },
        "warnings": [
            "注册策略不等于立即启用；启用需要第二次确认。",
            "启用策略不等于立即执行今天的订单。",
        ],
        "validation_results": {"strategy_config": status},
    }
    if validation_errors:
        return ToolResult(
            success=False,
            message="策略配置校验失败，未创建注册确认计划。",
            data=payload,
            errors=validation_errors,
            permission=ToolPermission.PREVIEW,
            tool_name="strategy_builder_tool",
        )

    plan = create_confirmation_plan(
        user_id,
        "register_strategy",
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
    write_agent_action_log(
        user_id,
        intent="strategy_change",
        tool_name="strategy_builder_tool",
        tool_input={"requirement": requirement, "parameters": params},
        tool_output_summary={
            "plan_id": plan["plan_id"],
            "strategy_id": manifest.strategy_id,
            "version": manifest.version,
        },
        plan_id=str(plan["plan_id"]),
        confirmation_status="pending",
        execution_status="preview_only",
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
            "requires_confirmation": True,
        }
    )
    return ToolResult(
        success=True,
        message="已生成长期策略注册预览；确认后只注册为禁用状态，启用还需要第二次确认。",
        data=data,
        warnings=list(payload["warnings"]),
        permission=ToolPermission.PREVIEW,
        tool_name="strategy_builder_tool",
        requires_confirmation=True,
        confirmation_token=str(plan["confirmation_token"]),
    )
