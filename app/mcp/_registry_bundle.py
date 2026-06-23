"""Phase 3+4 bundle-vocabulary MCP tools (new canonical names).

Extracted here to keep registry.py under the 600-line god-object guard.
The compat aliases (recipes_*/cookbook_*) remain in registry.py.  # compat-alias
"""

from __future__ import annotations

import mcp.types as types


def _bundle_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="bundle_list",
            description="List the caller's bundle and its skill provenance rows.",
            inputSchema={
                "type": "object",
                "properties": {"bundle_id": {"type": "string"}},
            },
        ),
        types.Tool(
            name="bundle_install",
            description=(
                "Install all skills from a bundle (bulk) or one skill by slug. "
                "bdl_token callers may omit bundle_id — it defaults to the "
                "token's scoped bundle. user/master callers must pass bundle_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "bundle_id": {
                        "type": "string",
                        "description": (
                            "Bundle UUID. Optional for bdl_token (defaults "
                            "to token's bundle_scope); required otherwise."
                        ),
                    },
                    "slug": {
                        "type": "string",
                        "description": (
                            "Optional single-skill filter. Omit to bulk-install "
                            "every active skill in the bundle."
                        ),
                    },
                },
            },
        ),
    ]
