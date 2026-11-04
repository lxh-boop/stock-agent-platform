from __future__ import annotations

import anyio
import asyncio
import json
from pathlib import Path
import threading
from typing import Any, Awaitable, Callable, TypeVar

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

from agent_graph.mcp.models import MCPServerConfig


T = TypeVar("T")


def _run_async(factory: Callable[[], Awaitable[T]]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return anyio.run(factory)

    result: list[T] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            result.append(anyio.run(factory))
        except BaseException as exc:  # pragma: no cover - only used by async callers
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True, name="mcp-stdio-bridge")
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


def _stdio_parameters(
    server: MCPServerConfig,
    context: dict[str, Any] | None,
) -> StdioServerParameters:
    if not server.command:
        raise RuntimeError(f"mcp_stdio_command_missing:{server.server_id}")
    runtime = dict(context or {})
    env = get_default_environment()
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    if runtime.get("db_path"):
        env["STOCK_MCP_DB_PATH"] = str(runtime["db_path"])
    if runtime.get("output_dir"):
        env["STOCK_MCP_OUTPUT_DIR"] = str(runtime["output_dir"])
    return StdioServerParameters(
        command=server.command,
        args=list(server.args),
        env=env,
        cwd=Path(server.cwd) if server.cwd else None,
        encoding="utf-8",
        encoding_error_handler="replace",
    )


async def _list_tools_async(
    server: MCPServerConfig,
    context: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    params = _stdio_parameters(server, context)
    with anyio.fail_after(float(server.timeout_seconds)):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=float(server.timeout_seconds),
            ) as session:
                await session.initialize()
                response = await session.list_tools()
    return [tool.model_dump(by_alias=True, exclude_none=True) for tool in response.tools]


def list_stdio_tools(
    server: MCPServerConfig,
    *,
    context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if server.transport != "stdio":
        raise RuntimeError(
            f"unsupported_mcp_transport:{server.transport}:{server.server_id}"
        )
    return _run_async(lambda: _list_tools_async(server, context))


def _text_payload(content: list[Any]) -> Any:
    texts = [
        str(getattr(item, "text", ""))
        for item in content
        if str(getattr(item, "type", "")) == "text"
        and str(getattr(item, "text", ""))
    ]
    if not texts:
        return {}
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except json.JSONDecodeError:
        return {"text": joined}


async def _call_tool_async(
    server: MCPServerConfig,
    tool_name: str,
    arguments: dict[str, Any],
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    params = _stdio_parameters(server, context)
    with anyio.fail_after(float(server.timeout_seconds)):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=float(server.timeout_seconds),
            ) as session:
                await session.initialize()
                response = await session.call_tool(
                    tool_name,
                    arguments=arguments,
                    read_timeout_seconds=float(server.timeout_seconds),
                )
    structured = getattr(response, "structured_content", None)
    payload = structured if structured is not None else _text_payload(response.content)
    if isinstance(payload, dict):
        result = dict(payload)
    else:
        result = {"data": payload}
    if getattr(response, "is_error", False):
        result.setdefault("success", False)
        result.setdefault("errors", ["mcp_server_tool_error"])
    return result


def call_stdio_tool(
    server: MCPServerConfig,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if server.transport != "stdio":
        raise RuntimeError(
            f"unsupported_mcp_transport:{server.transport}:{server.server_id}"
        )
    return _run_async(
        lambda: _call_tool_async(server, tool_name, arguments, context)
    )
