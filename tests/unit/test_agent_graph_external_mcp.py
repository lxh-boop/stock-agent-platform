"""External MCP (curated research-docs filesystem) integration tests.

Offline: no real npx spawn — the graceful paths are exercised directly and the
loader path is monkeypatched.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

from agent_graph.tools.external_mcp import (
    RESEARCH_DOCS_DIR_ENV,
    build_research_docs_tools,
)


def test_missing_dir_degrades_gracefully(monkeypatch, tmp_path):
    monkeypatch.setenv(RESEARCH_DOCS_DIR_ENV, str(tmp_path / "nope"))
    warnings: list[str] = []
    assert build_research_docs_tools(warnings) == []
    assert any("不存在" in w for w in warnings)


def test_empty_dir_degrades_gracefully(monkeypatch, tmp_path):
    root = tmp_path / "research"
    root.mkdir()
    monkeypatch.setenv(RESEARCH_DOCS_DIR_ENV, str(root))
    warnings: list[str] = []
    assert build_research_docs_tools(warnings) == []
    assert any("为空" in w for w in warnings)


def test_loader_returns_allowlisted_prefixed_tools(monkeypatch, tmp_path):
    root = tmp_path / "research"
    root.mkdir()
    (root / "茅台研报.md").write_text("研报内容", encoding="utf-8")
    monkeypatch.setenv(RESEARCH_DOCS_DIR_ENV, str(root))

    def fake_tool(name: str):
        tool = types.SimpleNamespace()
        tool.name = name
        return tool

    class FakeClient:
        def __init__(self, connections, **kwargs):
            captured["connections"] = connections
            captured["kwargs"] = kwargs

        async def get_tools(self):
            return [
                fake_tool("research_docs_read_file"),
                fake_tool("research_docs_list_directory"),
                fake_tool("research_docs_write_file"),  # must be filtered out
                fake_tool("unrelated_tool"),  # must be filtered out
            ]

    captured: dict[str, object] = {}
    fake_module = types.ModuleType("langchain_mcp_adapters")
    fake_client_module = types.ModuleType("langchain_mcp_adapters.client")
    fake_client_module.MultiServerMCPClient = FakeClient
    fake_module.client = fake_client_module
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", fake_module)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", fake_client_module)

    tools = build_research_docs_tools([])
    names = {tool.name for tool in tools}
    assert names == {"research_docs_read_file", "research_docs_list_directory"}
    connections = captured["connections"]
    stdio = connections["research_docs"]
    assert stdio["transport"] == "stdio"
    assert str(root.resolve()) in stdio["args"][-1]


def test_env_override_points_loader_at_directory(monkeypatch, tmp_path):
    root = tmp_path / "custom"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv(RESEARCH_DOCS_DIR_ENV, str(root))

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, connections, **kwargs):
            captured["connections"] = connections

        async def get_tools(self):
            return []

    fake_module = types.ModuleType("langchain_mcp_adapters")
    fake_client_module = types.ModuleType("langchain_mcp_adapters.client")
    fake_client_module.MultiServerMCPClient = FakeClient
    fake_module.client = fake_client_module
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters", fake_module)
    monkeypatch.setitem(sys.modules, "langchain_mcp_adapters.client", fake_client_module)

    assert build_research_docs_tools([]) == []
    stdio = captured["connections"]["research_docs"]
    assert str(root.resolve()) in stdio["args"][-1]
