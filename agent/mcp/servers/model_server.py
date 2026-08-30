from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from agent.mcp.adapters import ModelMCPAdapter
from agent.mcp.tool_envelope import MCPToolEnvelope
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def create_model_server(*, db_path: str | Path | None = None) -> MCPServer:
    adapter = ModelMCPAdapter(db_path=db_path)
    server = MCPServer(
        name="stock-daily-model",
        title="Stock Daily Internal Model MCP",
        description=(
            "Read completed Kronos inference snapshots. Full inference is owned "
            "by the asynchronous Task Runtime and cannot run in this request."
        ),
        version="1.0.0",
    )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def predict_stock_score(stock_code: str) -> MCPToolEnvelope:
        """Return one stock's latest completed real-model inference score."""
        return MCPToolEnvelope.from_payload(adapter.predict_stock_score(stock_code))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def predict_rank(top_k: int = 10) -> MCPToolEnvelope:
        """Return the latest completed real-model inference ranking."""
        return MCPToolEnvelope.from_payload(adapter.predict_rank(top_k))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def predict_risk(stock_code: str) -> MCPToolEnvelope:
        """Return risk fields produced by the latest completed model run."""
        return MCPToolEnvelope.from_payload(adapter.predict_risk(stock_code))

    return server


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    args = parser.parse_args()
    create_model_server().run("stdio")


if __name__ == "__main__":
    main()
