from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from agent.mcp.adapters import RagMCPAdapter
from agent.mcp.tool_envelope import MCPToolEnvelope
from config import OUTPUT_DIR


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def create_rag_server(
    *,
    db_path: str | Path | None = None,
    output_dir: str | Path = OUTPUT_DIR,
) -> MCPServer:
    adapter = RagMCPAdapter(db_path=db_path, output_dir=output_dir)
    server = MCPServer(
        name="stock-daily-rag",
        title="Stock Daily Internal RAG MCP",
        description=(
            "Read-only access to the existing BM25, dense retrieval, RRF, and "
            "reranker evidence service."
        ),
        version="1.0.0",
    )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def search_documents(
        query: str,
        stock_code: str = "",
        top_k: int = 5,
    ) -> MCPToolEnvelope:
        """Search indexed documents, optionally restricted to one stock."""
        return MCPToolEnvelope.from_payload(adapter.search_documents(query, stock_code, top_k))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def search_news(
        stock_code: str,
        as_of_date: str = "",
        limit: int = 10,
    ) -> MCPToolEnvelope:
        """Read mapped news evidence for one stock from the database."""
        return MCPToolEnvelope.from_payload(adapter.search_news(stock_code, as_of_date, limit))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def retrieve_evidence(
        stock_code: str,
        query: str = "",
        as_of_date: str = "",
        top_k: int = 5,
    ) -> MCPToolEnvelope:
        """Retrieve and merge database news with hybrid RAG evidence."""
        return MCPToolEnvelope.from_payload(adapter.retrieve_evidence(stock_code, query, as_of_date, top_k))

    return server


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("STOCK_MCP_OUTPUT_DIR") or str(OUTPUT_DIR),
    )
    args = parser.parse_args()
    create_rag_server(output_dir=args.output_dir).run("stdio")


if __name__ == "__main__":
    main()
