"""Wrap legacy worker ``ToolDefinition`` contracts as LangChain tools.

Every legacy handler obeys ``execution_handler(arguments: dict, context: dict)
-> dict``, so one adapter covers the whole registry. Argument schemas are
derived from each definition's JSON Schema; write-class tools pass through a
LangGraph ``interrupt()`` approval gate before executing, preserving the
legacy two-phase confirmation semantics inside the new engine.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langgraph.types import interrupt
from pydantic import BaseModel, Field, create_model

from agent_graph.toolbox.contracts import OP_WRITE, ToolDefinition

_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")
_MAX_TOOL_NAME_LENGTH = 64

_JSON_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def sanitize_tool_name(tool_id: str) -> str:
    """Map legacy dotted ids (``evidence.search_news``) to function-name-safe ids."""

    name = _NAME_PATTERN.sub("_", str(tool_id or "tool").strip())
    return name[:_MAX_TOOL_NAME_LENGTH] or "tool"


def _python_type(prop_schema: dict[str, Any]) -> Any:
    prop_type = str(prop_schema.get("type") or "string").lower()
    if prop_type == "array":
        items = prop_schema.get("items")
        if isinstance(items, dict) and str(items.get("type") or "").lower() == "object":
            return list[dict[str, Any]]
        if isinstance(items, dict) and str(items.get("type") or "").lower() == "string":
            return list[str]
        return list[Any]
    if prop_type == "object":
        return dict[str, Any]
    enum = prop_schema.get("enum")
    if enum:
        return LiteralTuple(tuple(str(item) for item in enum))
    return _JSON_TYPES.get(prop_type, str)


def LiteralTuple(values: tuple[str, ...]) -> Any:  # noqa: N802 - small helper
    from typing import Literal

    return Literal[values]  # type: ignore[valid-type]


def args_model_from_schema(tool_id: str, input_schema: dict[str, Any]) -> type[BaseModel]:
    """Build a Pydantic model from a tool definition's JSON Schema subset."""

    properties = dict((input_schema or {}).get("properties") or {})
    required = set((input_schema or {}).get("required") or [])
    if not properties:
        # Contract-light tool: accept a free-form arguments object.
        return create_model(
            sanitize_tool_name(tool_id) + "_args",
            arguments=(dict[str, Any], Field(default_factory=dict, description="工具参数对象")),
        )
    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        prop_schema = prop_schema if isinstance(prop_schema, dict) else {}
        annotation = _python_type(prop_schema)
        description = str(prop_schema.get("description") or "")
        if prop_name in required:
            fields[prop_name] = (annotation, Field(..., description=description))
        else:
            fields[prop_name] = (annotation | None, Field(default=None, description=description))
    return create_model(sanitize_tool_name(tool_id) + "_args", **fields)


def _normalize_raw_result(raw: Any, tool_id: str) -> dict[str, Any]:
    """Mirror ``tool_runtime.validation.normalise_raw_result`` essentials."""

    if isinstance(raw, dict) and "success" in raw:
        return raw
    if isinstance(raw, dict):
        return {"success": True, "message": "", "data": raw}
    return {
        "success": False,
        "message": f"工具 {tool_id} 返回了不可解析的结果",
        "data": {},
    }


def wrap_tool_definition(
    definition: ToolDefinition,
    context: dict[str, Any],
    *,
    worker_id: str = "",
) -> BaseTool:
    """Create one LangChain StructuredTool bound to a legacy execution handler."""

    requires_approval = bool(definition.requires_approval) or str(definition.operation_type) == OP_WRITE
    args_model = args_model_from_schema(definition.name, definition.input_schema)

    def _execute(arguments: dict[str, Any]) -> dict[str, Any]:
        if requires_approval:
            decision = interrupt(
                {
                    "action": "write_approval",
                    "tool_name": definition.name,
                    "worker_id": worker_id,
                    "args": arguments,
                    "summary": definition.description.splitlines()[0] if definition.description else definition.name,
                }
            )
            approved = isinstance(decision, dict) and str(decision.get("decision") or "").lower() == "approved"
            if not approved:
                return {
                    "success": False,
                    "rejected": True,
                    "message": f"写操作 {definition.name} 未获用户批准，已取消执行",
                    "data": {"tool_name": definition.name, "decision": "rejected"},
                }
        try:
            raw = definition.execution_handler(dict(arguments or {}), dict(context))
            result = _normalize_raw_result(raw, definition.name)
        except Exception as exc:  # noqa: BLE001 - tool failures flow back to the agent loop
            result = {
                "success": False,
                "message": f"工具 {definition.name} 执行失败",
                "data": {},
                "errors": [str(exc)[:500]],
            }
        # Keep tool payloads compact for the LLM context window.
        if isinstance(result.get("data"), dict):
            serialized = json.dumps(result["data"], ensure_ascii=False, default=str)
            if len(serialized) > 24000:
                result = {
                    **result,
                    "data": {
                        "truncated": True,
                        "note": "结果过大已截断，仅保留摘要字段",
                        "success_count": result["data"].get("success_count"),
                        "returned_count": result["data"].get("returned_count"),
                    },
                }
        return result

    return StructuredTool.from_function(
        func=_execute,
        name=sanitize_tool_name(definition.name),
        description=str(definition.description or definition.display_name or definition.name).strip(),
        args_schema=args_model,
    )


__all__ = ["wrap_tool_definition", "sanitize_tool_name", "args_model_from_schema"]
