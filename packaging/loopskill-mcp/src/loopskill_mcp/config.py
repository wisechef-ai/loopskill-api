"""Config resolution for the LoopSkill MCP stdio bridge.

Env-var names and precedence mirror the promise documented in this repo:
``docs/SELF_HOST.md`` ("Environment variables" section) and
``docs/recipes-skill/SKILL.md`` ("Claude Desktop config block") — both name
``LOOPSKILL_API_KEY`` as the canonical stdio env var, with
``MCP_LOOPSKILL_API_KEY`` reserved for the Hermes wizard integration and the
legacy ``RECIPES_API_KEY`` / ``MCP_RECIPES_API_KEY`` names kept working
"during the transition" per the same doc. Anonymous (no key) is a supported
mode — SELF_HOST.md: "free skills... install with no key at all".
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

#: The hosted StreamableHTTP MCP endpoint, per app/mcp/streaming.py's
#: ``_build_streamable_http_mount`` (mounted at ``/api/mcp/http`` in
#: app/main.py) and confirmed live via ``/.well-known/mcp.json``
#: (``transport.url``). The trailing slash matches docs/SELF_HOST.md's
#: "Hermes MCP config block" / "the trailing slash is required" note.
DEFAULT_MCP_URL = "https://app.loopskill.io/api/mcp/http/"

# Order matters: first non-empty wins. Canonical name checked first so a
# user who only sets the documented LOOPSKILL_API_KEY always gets it, even
# if a stale legacy var is also present in their shell.
API_KEY_ENV_VARS: tuple[str, ...] = (
    "LOOPSKILL_API_KEY",
    "MCP_LOOPSKILL_API_KEY",
    "RECIPES_API_KEY",
    "MCP_RECIPES_API_KEY",
)


@dataclass(frozen=True)
class BridgeConfig:
    """Resolved runtime configuration for one bridge process."""

    mcp_url: str
    api_key: str | None


def resolve_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """Resolve the LoopSkill API key from env vars, canonical name first.

    Returns ``None`` (anonymous mode) when no supported var is set — this is
    a normal, documented mode, not an error.
    """
    source: Mapping[str, str] = env if env is not None else os.environ
    for var in API_KEY_ENV_VARS:
        value = source.get(var)
        if value:
            return value
    return None


def load_config(env: Mapping[str, str] | None = None) -> BridgeConfig:
    """Build the full :class:`BridgeConfig` from the environment.

    ``LOOPSKILL_MCP_URL`` is an escape hatch (not part of the public
    promise) for pointing the bridge at a self-hosted LoopSkill instance —
    see docs/SELF_HOST.md's "Claude Desktop config block" / "your-host"
    examples for the self-host use case this exists to serve.
    """
    source: Mapping[str, str] = env if env is not None else os.environ
    url = source.get("LOOPSKILL_MCP_URL") or DEFAULT_MCP_URL
    return BridgeConfig(mcp_url=url, api_key=resolve_api_key(source))
