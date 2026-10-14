"""ah_1014 — GET /.well-known/mcp/server-card.json is public and single-sourced.

THE DEFECT (live-verified 2026-10-14)
-------------------------------------
``curl https://app.loopskill.io/.well-known/mcp/server-card.json`` -> 401
``{"detail":"Invalid or missing x-api-key header"}``. MCP directories (Smithery
first) list a server by scanning ``tools/list``; ours is x-api-key gated, so the
scan dead-ends at 401 and the hosted server (#393, streamable HTTP, already in
the official MCP Registry) cannot be listed. Smithery's documented bypass is a
static server card (SEP-1649) at exactly this path.

WHAT IS PINNED
--------------
* served with NO credential (200, JSON) through the REAL app + middleware;
* the path is on ``EXEMPT_PATHS`` (the allowlist that 401'd it);
* ``tools`` is exactly ``_tool_definitions()`` — same names, same order, same
  input schemas — so the card can never drift from what tools/list returns;
* ``serverInfo`` matches the name/version the live server answers initialize with;
* the card carries no key material.

RED-PROOF: against the pre-fix tree the route does not exist and the path is
not exempt, so every test here fails (401 / ImportError / membership).
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import create_app
from app.middleware._public_paths import EXEMPT_PATHS

PATH = "/.well-known/mcp/server-card.json"


def _client(db_session) -> TestClient:
    """Real app, raw client, NO x-api-key (the conftest client pre-seeds one
    and would mask a middleware regression). Mirrors test_gap_aiplugin_wellknown."""
    app = create_app()

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, raise_server_exceptions=True)


def test_200_with_no_credential(db_session):
    resp = _client(db_session).get(PATH)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.headers.get("etag")


def test_path_is_pinned_in_exempt_paths():
    assert PATH in EXEMPT_PATHS


def test_tools_are_exactly_the_registry(db_session):
    from app.mcp.registry import _tool_definitions

    card = _client(db_session).get(PATH).json()
    registry = _tool_definitions()
    assert registry, "registry is empty — the card would be decoration"
    assert [t["name"] for t in card["tools"]] == [t.name for t in registry]
    for served, tool in zip(card["tools"], registry):
        assert served["inputSchema"] == tool.inputSchema, tool.name
        assert served.get("description") == tool.description, tool.name


def test_server_info_matches_live_server(db_session):
    from app.mcp.server import SERVER_NAME, SERVER_VERSION

    card = _client(db_session).get(PATH).json()
    assert card["serverInfo"]["name"] == SERVER_NAME
    assert card["serverInfo"]["version"] == SERVER_VERSION
    assert card["authentication"]["required"] is True
    assert card["authentication"]["header"] == "x-api-key"
    assert card["transport"]["url"].endswith("/api/mcp/http")


def test_card_carries_no_key_material(db_session):
    body = json.dumps(_client(db_session).get(PATH).json())
    for marker in ("rec_agent_", "sk_live", "sk_test", "Bearer "):
        assert marker not in body
