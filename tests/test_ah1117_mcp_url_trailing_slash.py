"""ah_1117 — every advertised MCP transport URL carries the trailing slash.

THE DEFECT (live-verified 2026-11-17 21:0x CET, valid key)
-----------------------------------------------------------
``POST https://app.loopskill.io/api/mcp/http``  -> 307 Location: .../api/mcp/http/
``POST https://app.loopskill.io/api/mcp/http/`` -> 200

The StreamableHTTP app is a Starlette ``Mount("/api/mcp/http")``, so the bare
path only answers with a redirect. ``/skill`` and the MCP Registry listing
(tests/test_mcp_registry_listing.py) already say "the trailing slash is
required", yet every machine-readable surface an agent reads first
(mcp.json, server card, agent.json, ai-plugin.json, register / first-key
responses, config blocks) advertised the bare path. A client that follows the
descriptor literally and does not replay a POST across a 307 dead-ends on its
first call.

WHAT IS PINNED
--------------
* each well-known document, served through the REAL app with no credential,
  advertises ``.../api/mcp/http/``;
* the first-key reveal constant, the config-block default and the
  registration ``endpoints.mcp`` do the same;
* the bare path stays mounted (still answers, never 404), so old configs keep
  working through the redirect.

RED-PROOF: on the pre-fix tree every advertised-URL assertion fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import create_app

ROOT = Path(__file__).resolve().parents[1]
CANON = "/api/mcp/http/"


@pytest.fixture()
def raw_client(db_session):
    """Real app, NO x-api-key (mirrors test_ah1014_mcp_server_card._client)."""
    app = create_app()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, raise_server_exceptions=True)


def _mcp_urls(path: str, doc: dict) -> list[str]:
    if path == "/.well-known/mcp.json":
        return [doc["transport"]["url"]]
    if path == "/.well-known/mcp/server-card.json":
        return [doc["transport"]["url"]]
    if path == "/.well-known/agent.json":
        return [doc["mcp"]["endpoint"]]
    if path == "/.well-known/ai-plugin.json":
        return [doc["api"]["endpoint"]]
    raise AssertionError(path)


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/mcp.json",
        "/.well-known/mcp/server-card.json",
        "/.well-known/agent.json",
        "/.well-known/ai-plugin.json",
    ],
)
def test_wellknown_docs_advertise_trailing_slash(raw_client, path):
    r = raw_client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    for url in _mcp_urls(path, r.json()):
        assert url.endswith(CANON), f"{path} advertises {url!r} (bare path 307s)"


def test_first_key_and_config_blocks_advertise_trailing_slash():
    from app import first_key_routes
    from app._config_block_formatter import build_config_blocks

    assert first_key_routes._MCP_ENDPOINT.endswith(CANON)
    blocks = build_config_blocks(token="rec_live_x")
    for text in blocks.values():
        if "/api/mcp/http" in text:
            assert CANON in text, text


def test_registration_response_advertises_trailing_slash():
    src = (ROOT / "app/agent_registration_routes.py").read_text()
    assert '"mcp": f"{origin}/api/mcp/http/"' in src
    assert '"mcp": f"{origin}/api/mcp/http"' not in src


def test_bare_path_still_mounted(raw_client):
    # Old configs must keep working: the bare path redirects, never 404s.
    r = raw_client.post("/api/mcp/http", follow_redirects=False)
    assert r.status_code != 404
