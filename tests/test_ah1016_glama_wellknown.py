"""ah_1016 — GET /.well-known/glama.json is public and schema-valid.

THE DEFECT (live-verified 2026-10-16)
-------------------------------------
``curl https://app.loopskill.io/.well-known/glama.json`` -> 401
``{"detail":"Invalid or missing x-api-key header"}``. Glama already lists
io.github.wisechef-ai/loopskill (imported from the MCP Registry) as an
unclaimed, Unhealthy connector. The domain claim method fetches this document
anonymously, so the 401 made the listing unclaimable. That in turn left the
``missing-glama`` label on punkpeye/awesome-mcp-servers#13442.

WHAT IS PINNED
--------------
* served with NO credential (200, JSON) through the REAL app + middleware;
* the path is on ``EXEMPT_PATHS``;
* the body follows https://glama.ai/mcp/schemas/connector.json: ``$schema``
  plus ``claim`` (pattern-checked tokens) and/or ``maintainers`` (email);
* a malformed claim token is never published (it would void the document);
* nothing configured -> 404, never an invalid document.

RED-PROOF: against the pre-fix tree the path 401s, is not exempt, and
``build_glama_ownership`` does not exist, so every test here fails.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import create_app
from app.middleware._public_paths import EXEMPT_PATHS

PATH = "/.well-known/glama.json"
TOKEN = "glama_claim_" + "A" * 32


def _client(db_session) -> TestClient:
    """Real app, raw client, NO x-api-key (mirrors test_ah1014_mcp_server_card)."""
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
    body = resp.json()
    assert body["$schema"] == "https://glama.ai/mcp/schemas/connector.json"
    # schema anyOf: at least one of claim / maintainers
    assert "claim" in body or "maintainers" in body


def test_path_is_pinned_in_exempt_paths():
    assert PATH in EXEMPT_PATHS


def test_default_is_legacy_maintainer_email(monkeypatch):
    from app.agent_wellknown_routes import build_glama_ownership

    monkeypatch.setattr(settings, "GLAMA_CLAIM_TOKENS", [])
    monkeypatch.setattr(settings, "GLAMA_MAINTAINER_EMAIL", "hi@wisechef.ai")
    body = build_glama_ownership()
    assert body["maintainers"] == [{"email": "hi@wisechef.ai"}]
    assert "claim" not in body


def test_claim_token_published_and_malformed_dropped(monkeypatch):
    from app.agent_wellknown_routes import build_glama_ownership

    monkeypatch.setattr(settings, "GLAMA_CLAIM_TOKENS", [TOKEN, "glama_claim_short", "junk"])
    monkeypatch.setattr(settings, "GLAMA_MAINTAINER_EMAIL", "")
    body = build_glama_ownership()
    assert body["claim"] == TOKEN
    assert "maintainers" not in body

    second = "glama_claim_" + "b-_9" * 8
    monkeypatch.setattr(settings, "GLAMA_CLAIM_TOKENS", [TOKEN, second])
    assert build_glama_ownership()["claim"] == [TOKEN, second]


def test_nothing_configured_is_404_not_invalid_doc(db_session, monkeypatch):
    monkeypatch.setattr(settings, "GLAMA_CLAIM_TOKENS", ["not-a-token"])
    monkeypatch.setattr(settings, "GLAMA_MAINTAINER_EMAIL", "  ")
    assert _client(db_session).get(PATH).status_code == 404


def test_env_parsing_accepts_comma_and_json():
    from app.config import Settings

    assert Settings._parse_glama_tokens(f"{TOKEN}, ") == [TOKEN]
    assert Settings._parse_glama_tokens(f'["{TOKEN}"]') == [TOKEN]
    assert Settings._parse_glama_tokens("") == []
