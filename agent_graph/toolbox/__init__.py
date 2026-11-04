"""Shared contracts, registry, and validation for worker tools.

The legacy heavy executor (context bundles, artifact persistence, ReAct
integration) was removed with the self-built runtime; the LangGraph engine
invokes ``ToolDefinition.execution_handler`` directly.
"""

from .contracts import (
    AGENT_MAIN,
    AGENT_READ,
    AGENT_WRITE,
    OP_PROPOSAL,
    OP_READ,
    OP_SYSTEM,
    OP_WRITE,
    TOOL_IO_CONTRACT_VERSION,
    TOOL_RESULT_SCHEMA_VERSION,
    TOOL_VISIBILITY_PUBLIC,
    TOOL_VISIBILITY_SYSTEM_PRIVATE,
    TOOL_VISIBILITY_WORKER_PRIVATE,
    ToolDefinition,
    ToolError,
    ToolInputContract,
    ToolOutputContract,
    UnifiedToolResult,
)
from .registry import ToolRegistry
from .validation import (
    description,
    input_contracts_for,
    normalise_raw_result,
    output_contracts_for,
    result_schema,
    safe_argument_keys,
    schema,
    validate_input,
    validate_output,
)

__all__ = [
    "AGENT_MAIN",
    "AGENT_READ",
    "AGENT_WRITE",
    "OP_PROPOSAL",
    "OP_READ",
    "OP_SYSTEM",
    "OP_WRITE",
    "TOOL_IO_CONTRACT_VERSION",
    "TOOL_RESULT_SCHEMA_VERSION",
    "TOOL_VISIBILITY_PUBLIC",
    "TOOL_VISIBILITY_SYSTEM_PRIVATE",
    "TOOL_VISIBILITY_WORKER_PRIVATE",
    "ToolDefinition",
    "ToolError",
    "ToolInputContract",
    "ToolOutputContract",
    "ToolRegistry",
    "UnifiedToolResult",
    "description",
    "input_contracts_for",
    "normalise_raw_result",
    "output_contracts_for",
    "result_schema",
    "safe_argument_keys",
    "schema",
    "validate_input",
    "validate_output",
]
