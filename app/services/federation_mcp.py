"""REGISTER_MCP install path — federated MCP-server skills.

Until now ``InstallPath.REGISTER_MCP`` was a dead enum: ``route_install`` marked
it "always allowed; no rehost" (federation.py), but no adapter produced it and
the install route fell through to the honest-but-inert
"not yet executable here" stub. This module fills the EXECUTABLE half.

A REGISTER_MCP skill is NOT a SKILL.md you fetch and run locally — it is a
remote MCP **server** the agent registers in its client config. So "installing"
it means handing back a paste-ready config block (Hermes YAML + Claude Desktop
JSON) pointing at the server's own endpoint, plus an agent-friendly structured
``mcp_config`` dict.

Design parity with the FETCH_ORIGIN path:
  - FETCH_ORIGIN returns ``{content, raw_url, install_command}`` — the agent
    writes ``content`` to its skills dir.
  - REGISTER_MCP returns ``{mcp_config, install_command}`` — the agent merges
    ``mcp_config`` into its MCP client config.

NEVER rehosts and NEVER fabricates: the server endpoint comes straight from the
resolved skill's ``origin_url`` (or an explicit ``endpoint`` carried on the
row). No endpoint → caller surfaces an honest 409 (mirrors the
"fetch-origin not yet wired" branch), never a fake config.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

# Server-key sanitiser: MCP client config keys must be simple identifiers
# (no slashes/colons that would break YAML/JSON nesting or the client parser).
_KEY_SAFE = re.compile(r"[^a-z0-9_-]+")


def _server_key(slug: str, source: str) -> str:
    """Derive a stable, client-safe server key from a namespaced slug.

    ``github-anthropic--web-search`` → ``web-search``; falls back to the source
    when the leaf would be empty. Lowercased; non ``[a-z0-9_-]`` collapsed to
    ``-``; never empty.
    """
    leaf = (slug.rsplit("--", 1)[-1] or "").strip().lower()
    key = _KEY_SAFE.sub("-", leaf).strip("-")
    if not key:
        key = _KEY_SAFE.sub("-", (source or "mcp").lower()).strip("-") or "mcp"
    return key


def _is_http_endpoint(url: str) -> bool:
    """True only for a syntactically valid http(s) URL with a network host.

    Guards against handing back a config that points at a non-URL origin (e.g.
    a GitHub *page* URL, or an empty string) — those are not registrable MCP
    endpoints, so the caller must 409 rather than emit a broken config.
    """
    try:
        p = urlparse((url or "").strip())
    except (ValueError, AttributeError):
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def resolve_mcp_endpoint(skill) -> str | None:
    """Pick the registrable MCP endpoint for a REGISTER_MCP skill.

    Preference order:
      1. an explicit ``endpoint`` attribute (set by a future MCP-aware adapter),
      2. the skill's ``origin_url`` IF it is a real http(s) endpoint.

    Returns ``None`` when neither yields a valid http(s) URL — the signal for
    the caller to 409 honestly instead of fabricating a config.
    """
    explicit = getattr(skill, "endpoint", None)
    if isinstance(explicit, str) and _is_http_endpoint(explicit):
        return explicit.strip()
    origin = getattr(skill, "origin_url", "") or ""
    if _is_http_endpoint(origin):
        return origin.strip()
    return None


def build_mcp_server_config(skill, *, endpoint: str | None = None) -> dict:
    """Build the paste-ready MCP config block for a REGISTER_MCP external skill.

    Args:
        skill: the resolved ``ExternalSkill`` (install_path == REGISTER_MCP).
        endpoint: optional pre-resolved endpoint; falls back to
            ``resolve_mcp_endpoint(skill)`` when omitted.

    Returns a dict with:
        server_key:          the client-config key (e.g. "web-search")
        endpoint:            the resolved MCP server URL
        mcp_config:          structured {"mcpServers": {<key>: {"url": ...}}}
                             — an agent merges this into its client config.
        hermes_yaml:         Hermes ``mcp_servers:`` snippet (human paste)
        claude_desktop_json: Claude Desktop ``mcpServers`` JSON (human paste)
        install_command:     one-line human instruction

    Raises:
        ValueError: when no valid endpoint can be resolved. Callers translate
            this into a 409 (never a fabricated config).
    """
    ep = endpoint or resolve_mcp_endpoint(skill)
    if not ep:
        raise ValueError(
            f"REGISTER_MCP skill '{getattr(skill, 'slug', '?')}' has no "
            "registrable http(s) endpoint (origin_url is not a server URL)"
        )

    key = _server_key(getattr(skill, "slug", ""), getattr(skill, "source", ""))

    mcp_config = {"mcpServers": {key: {"url": ep}}}

    hermes_yaml = (
        "# Add to your Hermes config (hermes.yaml)\n" "mcp_servers:\n" f"  {key}:\n" f"    url: {ep}\n"
    )

    claude_desktop_json = json.dumps(
        {"mcpServers": {key: {"url": ep}}},
        indent=2,
    )

    return {
        "server_key": key,
        "endpoint": ep,
        "mcp_config": mcp_config,
        "hermes_yaml": hermes_yaml,
        "claude_desktop_json": claude_desktop_json,
        "install_command": (
            f"Register the '{key}' MCP server at {ep} in your client config "
            "(see hermes_yaml / claude_desktop_json)."
        ),
    }
