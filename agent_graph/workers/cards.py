"""Worker role constants used as tool-ownership metadata (``allowed_agent_types``).

These are plain identifiers, not agent definitions: the engine has a single
main agent; the constants keep tool-contract ownership stable across the
toolbox and the MCP admission table.
"""

EVIDENCE_COLLECTOR = "EVIDENCE_COLLECTOR"
PORTFOLIO_ANALYST = "PORTFOLIO_ANALYST"
GRAPH_RELATION_RETRIEVER = "GRAPH_RELATION_RETRIEVER"
DATABASE_WRITER = "DATABASE_WRITER"

__all__ = [
    "DATABASE_WRITER",
    "EVIDENCE_COLLECTOR",
    "GRAPH_RELATION_RETRIEVER",
    "PORTFOLIO_ANALYST",
]
