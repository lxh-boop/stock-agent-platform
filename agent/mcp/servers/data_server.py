from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from agent.mcp.adapters import DataMCPAdapter
from agent.mcp.tool_envelope import MCPToolEnvelope
from config import OUTPUT_DIR


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def create_data_server(
    *,
    db_path: str | Path | None = None,
    output_dir: str | Path = OUTPUT_DIR,
) -> MCPServer:
    adapter = DataMCPAdapter(db_path=db_path, output_dir=output_dir)
    server = MCPServer(
        name="stock-daily-data",
        title="Stock Daily Internal Data MCP",
        description="Read-only access to authoritative application data services.",
        version="1.0.0",
    )

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_user_profile(user_id: str) -> MCPToolEnvelope:
        """Read one user's profile, risk assessment, goals, and constraints."""
        return MCPToolEnvelope.from_payload(adapter.get_user_profile(user_id))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_portfolio_state(user_id: str) -> MCPToolEnvelope:
        """Read the current paper account, positions, orders, and consistency state."""
        return MCPToolEnvelope.from_payload(adapter.get_portfolio_state(user_id))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_positions(user_id: str) -> MCPToolEnvelope:
        """Read the user's current authoritative paper positions."""
        return MCPToolEnvelope.from_payload(adapter.get_positions(user_id))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_orders(user_id: str, limit: int = 200) -> MCPToolEnvelope:
        """Read the user's paper order history with a bounded result count."""
        return MCPToolEnvelope.from_payload(adapter.get_orders(user_id, limit))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_stock_info(
        stock_code: str,
        user_id: str = "default",
    ) -> MCPToolEnvelope:
        """Read stock metadata and the latest stored market snapshot."""
        return MCPToolEnvelope.from_payload(adapter.get_stock_info(stock_code, user_id))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_latest_ranking(
        top_k: int = 10,
        model_name: str = "",
    ) -> MCPToolEnvelope:
        """Read the latest model ranking from the authoritative database."""
        return MCPToolEnvelope.from_payload(adapter.get_latest_ranking(top_k, model_name))

    @server.tool(annotations=READ_ONLY, structured_output=True)
    def get_latest_recommendations(
        user_id: str,
        top_k: int = 50,
    ) -> MCPToolEnvelope:
        """Read the latest user-scoped final recommendations."""
        return MCPToolEnvelope.from_payload(adapter.get_latest_recommendations(user_id, top_k))

    return server


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("STOCK_MCP_OUTPUT_DIR") or str(OUTPUT_DIR),
    )
    args = parser.parse_args()
    create_data_server(
        db_path=args.db_path,
        output_dir=args.output_dir,
    ).run("stdio")


if __name__ == "__main__":
    main()
