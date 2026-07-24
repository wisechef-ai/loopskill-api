"""REGISTER_MCP federation install path — formatter + route + cookbook seam.

Until this change ``InstallPath.REGISTER_MCP`` was a dead enum: ``route_install``
marked it allowed but no producer emitted it and the install route fell through
to the inert "not yet executable here" stub. This suite pins the now-executable
behaviour:

  * ``app.services.federation_mcp.build_mcp_server_config`` — pure formatter,
    no network. Endpoint resolution, server-key sanitising, config-block shape,
    and the honest ValueError when no registrable endpoint exists.
  * ``GET /api/skills/external/{source}/{slug}/install`` — drives a REGISTER_MCP
    row through the public route via a seeded federation cache first_page (the
    route does ExternalSkill.from_dict on a cache hit), asserting the response
    carries a real mcp_config block, NOT the legacy stub note.

All deterministic — no live network (Mom-test discipline: cold-path validated
by hand against real surfaces separately).
"""

from __future__ import annotations

import pytest

from app.services.federation import ExternalSkill, InstallPath
from app.services.federation_mcp import (
    build_mcp_server_config,
    resolve_mcp_endpoint,
)


def _mcp_skill(
    *,
    slug: str = "acme--web-search",
    source: str = "lobehub",
    origin_url: str = "https://mcp.acme.dev/sse",
    endpoint: str | None = None,
) -> ExternalSkill:
    sk = ExternalSkill(
        slug=slug,
        title="Web Search MCP",
        source=source,
        install_path=InstallPath.REGISTER_MCP,
        origin_url=origin_url,
        license="MIT",
        redistributable=True,
        description="remote MCP server",
    )
    if endpoint is not None:
        # A future MCP-aware adapter may carry an explicit endpoint; ExternalSkill
        # is frozen, so attach via object.__setattr__ to simulate that field.
        object.__setattr__(sk, "endpoint", endpoint)
    return sk


# ───────────────────────────── formatter unit tests ─────────────────────


class TestResolveEndpoint:
    def test_origin_url_used_when_http(self):
        sk = _mcp_skill(origin_url="https://mcp.acme.dev/sse")
        assert resolve_mcp_endpoint(sk) == "https://mcp.acme.dev/sse"

    def test_explicit_endpoint_wins_over_origin(self):
        sk = _mcp_skill(
            origin_url="https://github.com/acme/page",  # a page, not a server
            endpoint="https://mcp.acme.dev/http",
        )
        assert resolve_mcp_endpoint(sk) == "https://mcp.acme.dev/http"

    def test_non_http_origin_yields_none(self):
        sk = _mcp_skill(origin_url="not-a-url")
        assert resolve_mcp_endpoint(sk) is None

    def test_empty_origin_yields_none(self):
        sk = _mcp_skill(origin_url="")
        assert resolve_mcp_endpoint(sk) is None


class TestBuildConfig:
    def test_happy_path_shape(self):
        sk = _mcp_skill(slug="acme--web-search", origin_url="https://mcp.acme.dev/sse")
        cfg = build_mcp_server_config(sk)
        assert cfg["server_key"] == "web-search"
        assert cfg["endpoint"] == "https://mcp.acme.dev/sse"
        assert cfg["mcp_config"] == {
            "mcpServers": {"web-search": {"url": "https://mcp.acme.dev/sse"}}
        }
        assert "mcp_servers:" in cfg["hermes_yaml"]
        assert "web-search" in cfg["hermes_yaml"]
        assert '"mcpServers"' in cfg["claude_desktop_json"]
        assert "web-search" in cfg["install_command"]

    def test_server_key_sanitised(self):
        # Slug leaf with unsafe chars collapses to [a-z0-9_-].
        sk = _mcp_skill(slug="acme--Web Search!! v2", origin_url="https://m.dev/sse")
        cfg = build_mcp_server_config(sk)
        assert cfg["server_key"] == "web-search-v2"

    def test_server_key_falls_back_to_source(self):
        # A slug whose leaf sanitises to empty falls back to the source.
        sk = _mcp_skill(slug="!!!", source="lobehub", origin_url="https://m.dev/sse")
        cfg = build_mcp_server_config(sk)
        assert cfg["server_key"] == "lobehub"

    def test_no_endpoint_raises_valueerror(self):
        sk = _mcp_skill(origin_url="not-a-url")
        with pytest.raises(ValueError):
            build_mcp_server_config(sk)

    def test_claude_json_is_valid_json(self):
        import json

        sk = _mcp_skill(origin_url="https://mcp.acme.dev/sse")
        cfg = build_mcp_server_config(sk)
        parsed = json.loads(cfg["claude_desktop_json"])
        assert parsed["mcpServers"]["web-search"]["url"] == "https://mcp.acme.dev/sse"


# ───────────────────────────── route integration ────────────────────────


class TestRegisterMcpRoute:
    def _seed_mcp_row(self, db_session, *, source="lobehub", slug="acme--web-search",
                      endpoint="https://mcp.acme.dev/sse"):
        """Seed the federation cache first_page with a REGISTER_MCP row.

        The install route resolves cache-first (read_first_page → from_dict), so
        a seeded row lets us exercise the route without a live adapter walk.
        """
        from app.services import federation_cache as fcache

        row = {
            "slug": slug,
            "title": "Web Search MCP",
            "source": source,
            "install_path": InstallPath.REGISTER_MCP.value,
            "origin_url": endpoint,
            "license": "MIT",
            "redistributable": True,
            "description": "remote MCP server",
        }
        fcache.write_source_cache(
            db_session, source, indexed_count=1, installable_count=1, first_page=[row]
        )

    def test_register_mcp_install_returns_config_block(self, client, db_session):
        self._seed_mcp_row(db_session)
        r = client.get("/api/skills/external/lobehub/acme--web-search/install")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["install_path"] == "register_mcp"
        assert body["server_key"] == "web-search"
        assert body["endpoint"] == "https://mcp.acme.dev/sse"
        assert body["mcp_config"] == {
            "mcpServers": {"web-search": {"url": "https://mcp.acme.dev/sse"}}
        }
        assert "mcp_servers:" in body["hermes_yaml"]
        # The legacy inert stub note must be GONE for this path.
        assert "not yet executable here" not in body.get("note", "")

    def test_register_mcp_without_endpoint_409(self, client, db_session):
        # origin_url that is not an http endpoint → honest 409, never a fake config.
        self._seed_mcp_row(db_session, endpoint="not-a-server-url")
        r = client.get("/api/skills/external/lobehub/acme--web-search/install")
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["install_path"] == "register_mcp"
        assert "no registrable" in detail["reason"].lower()
