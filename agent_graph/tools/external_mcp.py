"""External MCP integration: curated research-docs filesystem server.

Design boundary (ADR 0003): the only external MCP consumed is the read-only
filesystem server over a HUMAN-CURATED directory (``data/research`` by
default, override with ``STOCK_RESEARCH_DOCS_DIR``). The human decides what
enters the directory, so no unvetted web content reaches the agent. Tools are
loaded through the official ``langchain-mcp-adapters`` client, namespaced
with a ``research_docs_`` prefix, and skipped gracefully when Node/npx or the
directory is unavailable — a missing source never breaks a run.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from langchain_core.tools import BaseTool

RESEARCH_DOCS_DIR_ENV = "STOCK_RESEARCH_DOCS_DIR"
_TOOL_PREFIX = "research_docs"
# 只读允许清单：官方 filesystem server 的查询/读取类工具。
_ALLOWED_TOOL_NAMES = {
    "list_directory",
    "read_file",
    "read_multiple_files",
    "search_files",
    "directory_tree",
    "get_file_info",
}


def research_docs_dir() -> Path:
    raw = os.getenv(RESEARCH_DOCS_DIR_ENV) or str(Path("data") / "research")
    return Path(raw)


def build_research_docs_tools(warnings_out: list[str] | None = None) -> list[BaseTool]:
    """Load read-only tools over the curated research-docs directory.

    Returns an empty list (and appends a reason to ``warnings_out``) when the
    directory is absent/empty or the MCP server cannot be started.
    """

    def _warn(message: str) -> list[BaseTool]:
        if warnings_out is not None:
            warnings_out.append(message)
        return []

    root = research_docs_dir()
    try:
        entries = [item for item in root.iterdir()] if root.is_dir() else None
    except OSError:
        entries = None
    if entries is None:
        return _warn(f"研究文档目录不存在，未加载外部 MCP 工具: {root}")
    if not entries:
        return _warn(f"研究文档目录为空，未加载外部 MCP 工具: {root}")

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:
        return _warn(f"langchain-mcp-adapters 未安装，未加载外部 MCP 工具: {exc}")

    client = MultiServerMCPClient(
        {
            _TOOL_PREFIX: {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", str(root.resolve())],
                "transport": "stdio",
            }
        },
        tool_name_prefix=True,
    )
    try:
        tools = asyncio.run(client.get_tools())
    except Exception as exc:  # noqa: BLE001 - a missing source never breaks a run
        return _warn(f"外部研究文档 MCP 不可用，已跳过: {type(exc).__name__}: {str(exc)[:200]}")

    allowed: list[BaseTool] = []
    for tool in tools:
        base_name = str(tool.name).removeprefix(f"{_TOOL_PREFIX}_")
        if base_name in _ALLOWED_TOOL_NAMES:
            allowed.append(tool)
    if not allowed and warnings_out is not None:
        warnings_out.append("外部研究文档 MCP 已启动，但未暴露允许清单内的只读工具")
    return allowed


__all__ = ["build_research_docs_tools", "research_docs_dir", "RESEARCH_DOCS_DIR_ENV"]
