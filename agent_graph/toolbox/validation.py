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


def materialise_semantic_output_slots(
    definition: ToolDefinition,
    result: dict[str, Any],
) -> list[str]:
    """Map concrete Tool return paths to stable semantic output slots.

    The mapping is Runtime-only. Worker planners never see ``source_path`` and
    therefore do not depend on Python return-field names such as ``records``.
    """

    contracts = output_contracts_for(definition)
    if not contracts or not definition.output_contracts:
        return []
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    slots = dict(data.get("slots") or {}) if isinstance(data.get("slots"), dict) else {}
    slot_contracts = (
        dict(data.get("slot_contracts") or {})
        if isinstance(data.get("slot_contracts"), dict)
        else {}
    )
    published: list[str] = []
    envelope = {**dict(result), "data": data}
    for contract in contracts:
        found, value = _extract_path(envelope, contract.source_path)
        if not found:
            continue
        slots[str(contract.slot_id)] = deepcopy(value)
        slot_contracts[str(contract.slot_id)] = contract.contract_descriptor()
        published.append(str(contract.slot_id))
    if slots:
        data["slots"] = slots
        existing = [str(item) for item in data.get("produced_information_slots") or [] if str(item)]
        data["produced_information_slots"] = list(dict.fromkeys([*existing, *published]))
        data["slot_contracts"] = slot_contracts
        result["data"] = data
    return published


def validate_semantic_output_contracts(
    definition: ToolDefinition,
    result: dict[str, Any],
) -> list[str]:
    if not definition.output_contracts or not bool(result.get("success")):
        return []
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    slot_contracts = (
        data.get("slot_contracts")
        if isinstance(data.get("slot_contracts"), dict)
        else {}
    )
    errors = [
        f"missing_output_slot:{contract.slot_id}"
        for contract in definition.output_contracts
        if str(contract.slot_id) not in slots
    ]
    errors.extend(
        f"missing_output_contract:{contract.slot_id}"
        for contract in definition.output_contracts
        if str(contract.slot_id) not in slot_contracts
    )
    return errors


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


def validate_output(
    definition: ToolDefinition,
    result: dict[str, Any],
) -> list[str]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return [
        f"missing_output:{name}"
        for name in definition.output_schema.get("required_data_keys") or []
        if name not in data
    ]


def safe_argument_keys(arguments: dict[str, Any]) -> list[str]:
    safe: list[str] = []
    for key in sorted(arguments.keys()):
        lowered = str(key or "").lower()
        if any(
            marker in lowered
            for marker in ("confirmation_token", "api_key", "password", "secret", "token")
        ):
            safe.append("secret_arg")
        else:
            safe.append(str(key))
    return sorted(set(safe))
