"""evergreen_0206 Phase F2 — the external federation namespace must be PUBLIC.

Mom-test regression: a cold stranger (no x-api-key) must be able to BOTH
discover external skills AND run the fetch-origin install. The plain `client`
fixture skips APIKeyMiddleware, so it can't catch a 401 from the auth gate —
this suite wires the production middleware (build_test_app) and asserts the
external namespace is allowlisted. Network is mocked.

Before the fix, /api/skills/external/{source}/{slug}/install returned 401
because the path ends in the auth verb "install" and has slashes (failing the
single-segment public-skill-detail rule). The fix adds /api/skills/external to
PUBLIC_PREFIXES.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.services.federation_live as fl

_HERMES_HTML = """
<table><thead><tr><th>Skill</th><th>Description</th><th>Path</th></tr></thead>
<tbody>
<tr><td>arxiv</td><td>Search arXiv papers</td><td>research/arxiv</td></tr>
</tbody></table>
"""


@pytest.fixture
def mw_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def setup_function(_):
    fl._cache.clear()


def test_external_list_is_public_no_api_key(mw_client, monkeypatch):
    """GET /api/skills/external must be reachable with NO x-api-key header."""
    monkeypatch.setattr(fl, "_load_hermes_catalog", lambda: fl._parse_hermes_catalog(_HERMES_HTML))
    resp = mw_client.get("/api/skills/external")  # no headers
    assert resp.status_code == 200, f"external list must be public, got {resp.status_code}"
    assert "per_source" in resp.json()


def test_external_install_is_public_no_api_key(mw_client, monkeypatch):
    """GET /api/skills/external/{source}/{slug}/install must be public (the cold-path)."""
    monkeypatch.setattr(fl, "_load_hermes_catalog", lambda: fl._parse_hermes_catalog(_HERMES_HTML))
    monkeypatch.setattr(
        fl, "hermes_origin_skill_md", lambda slug: ("https://raw.example/SKILL.md", "# arxiv\nbody")
    )
    resp = mw_client.get("/api/skills/external/hermes-hub/research--arxiv/install")  # no headers
    assert resp.status_code != 401, (
        f"external install MUST be installable without auth — got {resp.status_code}: {resp.text[:200]}"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["install_path"] == "fetch_origin"
    assert body["content"] == "# arxiv\nbody"


def test_external_install_unknown_source_404_not_401(mw_client):
    """An unknown source 404s (honest) rather than 401 — proves the prefix is public."""
    resp = mw_client.get("/api/skills/external/lobehub/x/install")
    assert resp.status_code == 404
