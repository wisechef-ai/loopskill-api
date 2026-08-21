"""Config block formatter for cookbook share tokens.

Generates Hermes YAML and Claude Desktop JSON snippets that users can paste
directly into their client configuration to use a share token.

Format follows the QUICKSTART-share.md schema.
"""

from __future__ import annotations

import json


def build_config_blocks(
    *,
    token: str,
    cookbook_id: str | None = None,
    server_url: str = "https://app.loopskill.io/api/mcp/http",
) -> dict[str, str]:
    """Return Hermes YAML + Claude Desktop JSON snippets for an API key or share token.

    Originally share-token-only (cookbook_id required); flywheel F1.2 widened
    this to also serve the plain auto-minted `rec_` first key, where there is
    no cookbook scope to annotate — cookbook_id is optional for that reason.

    Args:
        token: Plaintext key or share token (rec_... or cbt_... format).
        cookbook_id: UUID string of the cookbook this token grants access to,
            or None for an unscoped account-level key.
        server_url: Base URL for the MCP server endpoint.

    Returns:
        dict with keys:
          hermes_yaml: YAML string for Hermes client config.
          claude_desktop_json: JSON string for Claude Desktop mcpServers block.
    """
    hermes_yaml = _build_hermes_yaml(token=token, cookbook_id=cookbook_id, server_url=server_url)
    claude_json = _build_claude_desktop_json(token=token, server_url=server_url)
    return {
        "hermes_yaml": hermes_yaml,
        "claude_desktop_json": claude_json,
    }


def _build_hermes_yaml(
    *,
    token: str,
    cookbook_id: str | None,
    server_url: str,
) -> str:
    """Build a Hermes YAML config snippet for the given key/share token."""
    cookbook_line = f"    # Cookbook: {cookbook_id}\n" if cookbook_id else ""
    return (
        "# Add to your Hermes config (hermes.yaml)\n"
        "mcp_servers:\n"
        "  loopskill:\n"
        f"    url: {server_url}\n"
        "    headers:\n"
        f"      x-api-key: {token}\n"
        f"{cookbook_line}"
    )


def _build_claude_desktop_json(
    *,
    token: str,
    server_url: str,
) -> str:
    """Build a Claude Desktop JSON mcpServers snippet for the given share token."""
    config = {
        "mcpServers": {
            "loopskill": {
                "url": server_url,
                "headers": {
                    "x-api-key": token,
                },
            }
        }
    }
    return json.dumps(config, indent=2)
