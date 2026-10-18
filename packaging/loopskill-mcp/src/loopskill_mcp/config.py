"""Config resolution for the LoopSkill MCP stdio bridge.

Env-var names and precedence mirror the promise documented in this repo:
``docs/SELF_HOST.md`` ("Environment variables" section) and
``docs/recipes-skill/SKILL.md`` ("Claude Desktop config block") — both name
``LOOPSKILL_API_KEY`` as the canonical stdio env var, with
``MCP_LOOPSKILL_API_KEY`` reserved for the Hermes wizard integration and the
legacy ``RECIPES_API_KEY`` / ``MCP_RECIPES_API_KEY`` names kept working
"during the transition" per the same doc. With no key the bridge
still starts (resolve returns None). The hosted endpoint answers 401 to every
keyless MCP request, so the bridge answers ``tools/list`` from the public
server card and turns ``tools/call`` into an actionable "get a free key" error.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

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


#: Path of the public, keyless static server card (app/routes/wellknown, #422).
#: It lists every tool with its inputSchema, so a keyless ``tools/list`` can be
#: answered from it instead of failing on the hosted endpoint's 401.
SERVER_CARD_PATH = "/.well-known/mcp/server-card.json"

#: Path of the keyless self-registration endpoint that mints a free API key.
REGISTER_PATH = "/api/agents/register"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class BridgeConfig:
    """Resolved runtime configuration for one bridge process."""

    mcp_url: str
    api_key: str | None
    #: Keyless tool catalogue; derived from ``mcp_url``'s origin when unset.
    server_card_url: str | None = None

    @property
    def card_url(self) -> str:
        return self.server_card_url or _origin(self.mcp_url) + SERVER_CARD_PATH

    @property
    def register_url(self) -> str:
        return _origin(self.mcp_url) + REGISTER_PATH


def resolve_api_key(env: Mapping[str, str] | None = None) -> str | None:
    """Resolve the LoopSkill API key from env vars, canonical name first.

    Returns ``None`` when no supported var is set. The bridge still starts,
    but the hosted MCP endpoint rejects keyless calls with 401
    (``Invalid or missing x-api-key header``); keyless use only applies to
    public REST endpoints or a self-hosted server configured to allow it.
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
    return BridgeConfig(
        mcp_url=url,
        api_key=resolve_api_key(source),
        server_card_url=source.get("LOOPSKILL_SERVER_CARD_URL") or None,
    )
