from __future__ import annotations

from typing import Any

from jsonschema import exceptions as jsonschema_exceptions
from jsonschema import validators

def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any] | None) -> tuple[bool, list[str]]:
    return validate_payload_schema(schema, dict(arguments or {}))


def validate_payload_schema(
    schema: dict[str, Any],
    payload: Any,
) -> tuple[bool, list[str]]:
    schema = dict(schema or {})
    try:
        validator_type = validators.validator_for(schema)
        validator_type.check_schema(schema)
        validator = validator_type(schema)
        failures = sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    except jsonschema_exceptions.SchemaError as exc:
        return False, [f"invalid_runtime_schema:{exc.message}"]
    errors: list[str] = []
    for failure in failures:
        path = ".".join(str(item) for item in failure.absolute_path) or "$"
        errors.append(f"schema_validation:{path}:{failure.validator}")
    return not errors, errors
