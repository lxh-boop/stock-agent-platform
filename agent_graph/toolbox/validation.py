"""Schema helpers and deterministic validation for registered tools."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .contracts import ToolDefinition, ToolInputContract, ToolOutputContract




def input_contracts_for(definition: ToolDefinition) -> list[ToolInputContract]:
    """Return explicit semantic inputs only.

    Worker-private Tool planning no longer infers semantic inputs from Python
    function schemas or non-contract metadata.
    """

    return list(definition.input_contracts or [])


def output_contracts_for(definition: ToolDefinition) -> list[ToolOutputContract]:
    """Return the explicit semantic outputs declared by the Tool.

    There is intentionally no produced_outputs -> semantic Slot fallback.
    Worker-private Tool data enters the new runtime only through concrete
    ToolOutputContract source_path mappings.
    """

    return list(definition.output_contracts or [])


def _extract_path(payload: dict[str, Any], path: str) -> tuple[bool, Any]:
    text = str(path or "").strip()
    if not text:
        return False, None
    current: Any = payload
    if text == "$":
        return True, payload
    if text.startswith("$."):
        text = text[2:]
    for part in [item for item in text.split(".") if item]:
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current




def schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required or []),
        "additionalProperties": True,
    }


def result_schema(required_data_keys: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "required_data_keys": list(required_data_keys or []),
    }


def description(
    function: str,
    applies: str,
    not_for: str,
    inputs: str,
    outputs: str,
    side_effects: str = "None; read-only.",
) -> str:
    return (
        f"Function: {function}\n"
        f"Applies when: {applies}\n"
        f"Not for: {not_for}\n"
        f"Preconditions: valid runtime context and required inputs.\n"
        f"Main inputs: {inputs}\n"
        f"Main outputs: {outputs}\n"
        f"Side effects: {side_effects}"
    )


def normalise_raw_result(
    raw: Any,
    *,
    requested_name: str,
    canonical_name: str,
) -> dict[str, Any]:
    if hasattr(raw, "to_dict"):
        payload = raw.to_dict()
    elif isinstance(raw, dict):
        payload = dict(raw)
    else:
        payload = {"success": True, "message": str(raw), "data": {}}

    data = payload.get("data")
    if not isinstance(data, dict):
        data = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "success",
                "message",
                "warnings",
                "errors",
                "tool_name",
                "permission",
                "disclaimer",
                "error_type",
                "error_message",
                "failure_kind",
                "retryable",
            }
        }
    warnings = payload.get("warnings") or []
    errors = payload.get("errors") or []
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    if not isinstance(errors, list):
        errors = [str(errors)]
    if "success" in payload:
        success = bool(payload.get("success"))
    elif str(payload.get("status") or "").lower() in {"success", "ok"}:
        success = True
    else:
        success = not bool(errors)
    return {
        "success": success,
        "message": str(payload.get("message") or ""),
        "data": data,
        "warnings": [str(item) for item in warnings if str(item).strip()],
        "errors": [str(item) for item in errors if str(item).strip()],
        "error_type": str(payload.get("error_type") or ""),
        "error_message": str(payload.get("error_message") or ""),
        "failure_kind": str(payload.get("failure_kind") or ""),
        "retryable": bool(payload.get("retryable", False)),
        "tool_name": requested_name,
        "canonical_tool_name": canonical_name,
    }


def validate_input(
    definition: ToolDefinition,
    arguments: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    for name in definition.input_schema.get("required") or []:
        if arguments.get(name) in (None, ""):
            errors.append(f"missing_required:{name}")
    properties = (
        definition.input_schema.get("properties")
        if isinstance(definition.input_schema.get("properties"), dict)
        else {}
    )
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for name, value in arguments.items():
        if value is None or name not in properties:
            continue
        wanted = properties.get(name, {}).get("type")
        allowed = type_map.get(str(wanted or ""))
        if allowed and not isinstance(value, allowed):
            errors.append(f"invalid_type:{name}:{wanted}")
    return errors



