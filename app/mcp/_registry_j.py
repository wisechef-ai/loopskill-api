"""Phase J tool definitions — split out to keep registry.py under 600 lines."""
from __future__ import annotations

import mcp.types as types


def _phase_j_tools() -> list[types.Tool]:
    """Return the Phase J (loopclose_3005) tool definitions."""
    return [
        types.Tool(
            name="recipes_configure_feedback",
            description=(
                "Configure per-cookbook feedback routing to the user's own GitHub repo. "
                "Pro/Pro+ only. Pass repo='owner/name', mode='pat', and a fine-grained "
                "GitHub PAT with issues:write to route feedback issues to your repo. "
                "Pass repo=None to clear and revert to default (wisechef-ai/recipes-api)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": (
                            "GitHub 'owner/name' repo to route feedback issues to. "
                            "Pass null/omit to clear custom routing."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["pat", "github_app"],
                        "description": (
                            "'pat' — store a fine-grained GitHub PAT (issues:write). "
                            "'github_app' — future App token (not yet live)."
                        ),
                    },
                    "pat": {
                        "type": "string",
                        "description": (
                            "Fine-grained GitHub PAT with issues:write on the target repo. "
                            "Required when mode='pat'. Never logged."
                        ),
                    },
                    "cookbook_id": {
                        "type": "string",
                        "description": "UUID of the cookbook to configure. Defaults to caller's personal cookbook.",
                    },
                },
            },
        ),
    ]
