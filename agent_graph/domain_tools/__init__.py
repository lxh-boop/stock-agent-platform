from __future__ import annotations

__all__ = ["ToolSpec", "get_tool_registry", "list_tools"]


def __getattr__(name: str):
    if name in __all__:
        from agent_graph.domain_tools.tool_registry import (
            ToolSpec,
            get_tool_registry,
            list_tools,
        )

        return {
            "ToolSpec": ToolSpec,
            "get_tool_registry": get_tool_registry,
            "list_tools": list_tools,
        }[name]
    raise AttributeError(name)
