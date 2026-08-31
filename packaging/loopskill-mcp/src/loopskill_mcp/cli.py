"""Console-script entry point for ``loopskill-mcp`` / ``python -m loopskill_mcp``."""

from __future__ import annotations

import anyio

from loopskill_mcp import __version__
from loopskill_mcp.config import load_config
from loopskill_mcp.server import run_stdio


def main() -> None:
    """Entry point registered as the ``loopskill-mcp`` console script."""
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in ("-V", "--version"):
        print(f"loopskill-mcp {__version__}")
        return
    config = load_config()
    anyio.run(run_stdio, config)


if __name__ == "__main__":
    main()
