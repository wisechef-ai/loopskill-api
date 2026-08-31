"""Issue #281 — the REST external-install route is a thin transport over the
shared typed resolver (``resolve_external_install_full``).

Two #280 security guards must hold ON THE REST ROUTE now that it consumes the
resolver (they were MCP-only before the unification):

  1. http(s)-only URL guard — a non-http(s) ``origin_url`` must be sanitized
     (dropped) in the response, and a fetch-origin ``install_command`` must
     never survive a rejected URL.
  2. REGISTER_MCP endpoint guard — a register-mcp skill whose origin_url is
     not a strict http(s)+host+no-controls URL must 409 (wiring_missing),
     never a fabricated config block.

All deterministic — no live network (cache-seeded / adapter-monkeypatched).
"""

from __future__ import annotations

import pytest

import app.services.federation_live as fl
from app.services.federation import InstallPath


# ─────────────── guard 1: http(s)-only URL rejection ───────────────


class TestHttpOnlyUrlGuard:
    def test_fetch_origin_rejects_non_http_url_and_drops_install_command(
        self, client, db_session, monkeypatch
    ):
        """A hermes-hub row whose origin_url is not http(s) must not leak the
        unsafe URL or an install_command built from it — the resolver's
        sanitize step drops both (fetch-origin path)."""
        from app.services import federation_cache as fcache

        row = {
            "slug": "research--arxiv",
            "title": "arxiv",
            "source": "hermes-hub",
            "install_path": InstallPath.FETCH_ORIGIN.value,
            "origin_url": "ftp://insecure.example/skills/arxiv",  # not http(s)
            "license": "MIT",
            "redistributable": True,
            "description": "search arxiv",
        }
        fcache.write_source_cache(
            db_session, "hermes-hub", indexed_count=1, installable_count=1, first_page=[row]
        )
        monkeypatch.setattr(
            fl,
            "hermes_origin_skill_md",
            lambda slug: ("ftp://insecure.example/SKILL.md", "# arxiv\nbody"),
        )
        r = client.get("/api/skills/external/hermes-hub/research--arxiv/install")
        assert r.status_code == 200, r.text
        body = r.json()
        # Unsafe URL is never propagated…
        assert body.get("origin_url") in (None, "")
        assert body.get("raw_url") in (None, "")
        # …and no install_command may be built from a rejected URL.
        assert "install_command" not in body
        # The issue-mandated attribution/content fields stay, but no URL FIELD
        # may carry the rejected scheme through to the agent.
        assert body.get("origin_url") in (None, "")

    def test_deep_link_payload_urls_are_scheme_guarded(self, client, db_session):
        """A deep-link (non-redistributable) row with a non-http(s) origin_url
        must not carry the unsafe URL through to the agent."""
        from app.services import federation_cache as fcache

        row = {
            "slug": "persona--shadow",
            "title": "shadow",
            "source": "lobehub",
            "install_path": InstallPath.DEEP_LINK.value,
            "origin_url": "javascript:alert(1)",
            "license": None,
            "redistributable": False,
            "description": "persona prompt",
        }
        fcache.write_source_cache(
            db_session, "lobehub", indexed_count=1, installable_count=1, first_page=[row]
        )
        r = client.get("/api/skills/external/lobehub/persona--shadow/install")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("installed") is False
        assert "agent_instructions" in body
        assert body.get("origin_url") in (None, "")


# ─────────── guard 2: REGISTER_MCP strict endpoint guard (REST) ───────────


class TestRegisterMcpEndpointGuardRest:
    def _seed(self, db_session, *, endpoint):
        from app.services import federation_cache as fcache

        row = {
            "slug": "acme--web-search",
            "title": "Web Search MCP",
            "source": "lobehub",
            "install_path": InstallPath.REGISTER_MCP.value,
            "origin_url": endpoint,
            "license": "MIT",
            "redistributable": True,
            "description": "remote MCP server",
        }
        fcache.write_source_cache(
            db_session, "lobehub", indexed_count=1, installable_count=1, first_page=[row]
        )

    def test_newline_injected_endpoint_is_409_never_config(self, client, db_session):
        """A control-char (newline) endpoint is config injection via YAML/JSON
        interpolation — REST must 409 wiring_missing, not emit a config."""
        self._seed(db_session, endpoint="https://good.example/sse\ncommand: pwn")
        r = client.get("/api/skills/external/lobehub/acme--web-search/install")
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert detail["install_path"] == "register_mcp"
        assert "no registrable" in detail["reason"].lower()
        # Never a fabricated config block — the payload is only the
        # {reason, install_path, origin_url, license} wiring-missing detail.
        assert set(detail) == {"reason", "install_path", "origin_url", "license"}

    def test_ftp_endpoint_is_409_never_config(self, client, db_session):
        self._seed(db_session, endpoint="ftp://mcp.acme.dev/sse")
        r = client.get("/api/skills/external/lobehub/acme--web-search/install")
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "mcp_config" not in str(detail)

    def test_valid_endpoint_still_returns_config(self, client, db_session):
        """Sanity: the guard only bites unsafe endpoints; a good one still 200s."""
        self._seed(db_session, endpoint="https://mcp.acme.dev/sse")
        r = client.get("/api/skills/external/lobehub/acme--web-search/install")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["mcp_config"] == {
            "mcpServers": {"web-search": {"url": "https://mcp.acme.dev/sse"}}
        }
