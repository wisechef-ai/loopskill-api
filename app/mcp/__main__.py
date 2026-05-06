"""``python -m app.mcp`` — run the Recipes MCP server on stdio."""

from __future__ import annotations

import asyncio

from app.mcp.server import run_stdio


def main() -> None:
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()
