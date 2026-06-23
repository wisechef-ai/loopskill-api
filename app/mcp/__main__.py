"""``python -m app.mcp`` — run the Recipes MCP server on stdio."""

from __future__ import annotations

import asyncio

from app.mcp.server import run_stdio


def main() -> None:
    """Entry point: run the MCP server over stdio."""
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
