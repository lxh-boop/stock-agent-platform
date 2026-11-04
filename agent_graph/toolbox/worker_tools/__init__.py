"""Run-scoped private tools available only to explicitly authorized Workers.

Exports are lazy so Tool-DAG contract code can load the private directory
without importing every business integration and database adapter.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "EVIDENCE_FINALIZE_COLLECTION_TOOL": (".evidence", "EVIDENCE_FINALIZE_COLLECTION_TOOL"),
    "EVIDENCE_SEARCH_NEWS_TOOL": (".evidence", "EVIDENCE_SEARCH_NEWS_TOOL"),
    "EVIDENCE_SEARCH_RAG_TOOL": (".evidence", "EVIDENCE_SEARCH_RAG_TOOL"),
    "build_evidence_tool_definitions": (".evidence", "build_evidence_tool_definitions"),
    "GRAPH_RELATION_FIND_PATHS": (".graph_relation", "GRAPH_RELATION_FIND_PATHS"),
    "GRAPH_RELATION_READ_NEIGHBORHOOD": (".graph_relation", "GRAPH_RELATION_READ_NEIGHBORHOOD"),
    "build_graph_relation_tool_definitions": (".graph_relation", "build_graph_relation_tool_definitions"),
    "DATABASE_WRITE_EVIDENCE_GRAPH_CONTEXT": (".graph_context", "DATABASE_WRITE_EVIDENCE_GRAPH_CONTEXT"),
    "DATABASE_WRITE_PORTFOLIO_GRAPH_CONTEXT": (".graph_context", "DATABASE_WRITE_PORTFOLIO_GRAPH_CONTEXT"),
    "build_graph_context_tool_definitions": (".graph_context", "build_graph_context_tool_definitions"),
    "INTERNAL_ACCOUNT_GET_STATE": (".internal_system", "INTERNAL_ACCOUNT_GET_STATE"),
    "INTERNAL_BACKTEST_GET_SUMMARY": (".internal_system", "INTERNAL_BACKTEST_GET_SUMMARY"),
    "INTERNAL_MODEL_GET_METRICS": (".internal_system", "INTERNAL_MODEL_GET_METRICS"),
    "INTERNAL_PORTFOLIO_GET_STATE": (".internal_system", "INTERNAL_PORTFOLIO_GET_STATE"),
    "INTERNAL_PREDICTION_GET_STOCK": (".internal_system", "INTERNAL_PREDICTION_GET_STOCK"),
    "INTERNAL_RANKING_GET_LATEST": (".internal_system", "INTERNAL_RANKING_GET_LATEST"),
    "INTERNAL_STRATEGY_GET_SELECTED": (".internal_system", "INTERNAL_STRATEGY_GET_SELECTED"),
    "INTERNAL_USER_PROFILE_GET": (".internal_system", "INTERNAL_USER_PROFILE_GET"),
    "build_internal_system_tool_definitions": (".internal_system", "build_internal_system_tool_definitions"),
    "WorkerToolDirectory": (".registry", "WorkerToolDirectory"),
    "build_worker_tool_registry": (".registry", "build_worker_tool_registry"),
}


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attr_name = target
    value = getattr(import_module(module_name, __name__), attr_name)
    globals()[name] = value
    return value


__all__ = list(_EXPORTS)
