"""tests/test_fleetos_T_trojan_skill.py — fleetos_1607 Phase T gate suite.

RED-proofs the trojan skill (the fleet control-plane front door):
  * GET /fleet/skill returns 200 text/plain WITHOUT a key (cold-agent, public).
  * the served body is the real fleet SKILL.md (frontmatter + endpoints + auth +
    install steps + security notes).
  * the route exists in the REAL create_app() route table (not just the test shim).
  * the /fleet/skill prefix is pinned in the middleware public-path allow-list.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests._app_factory import build_test_app


def _fleet_client(db_session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch, with_middleware=True)
    return TestClient(app)


def test_fleet_skill_served_public_no_key(db_session, monkeypatch):
    """A cold agent with NO api-key gets the fleet skill (200, text/plain)."""
    client = _fleet_client(db_session, monkeypatch)
    resp = client.get("/fleet/skill")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # frontmatter + the load-bearing sections a client needs
    assert "name: loopskill-fleet" in body
    assert "control plane for AI agent fleets" in body
    assert "loopskill_enroll_member" in body
    assert "loopskill_reconcile_status" in body
    assert "loopskill_report_run" in body
    assert "loopskill_harvest" in body
    assert "loopskill_assign" in body
    assert "x-api-key" in body
    assert "Security notes" in body


def test_fleet_skill_alt_paths(db_session, monkeypatch):
    client = _fleet_client(db_session, monkeypatch)
    for path in ("/fleet/skill/", "/fleet/SKILL.md"):
        resp = client.get(path)
        assert resp.status_code == 200
        assert "loopskill-fleet" in resp.text


def test_fleet_skill_route_in_real_create_app():
    """The route exists in the REAL app, not just the test factory shim."""
    import os

    os.environ.setdefault("WR_API_KEY", "test-key")
    os.environ.setdefault("WR_SIGNING_SECRET", "test-secret")
    os.environ.setdefault("WR_JWT_SECRET", "test-jwt")
    os.environ.setdefault("WR_HEARTBEAT_PEPPER", "test-pepper")
    from app.main import create_app

    app = create_app()
    paths = {r.path for r in app.routes}
    assert "/fleet/skill" in paths
    assert "/fleet/SKILL.md" in paths


def test_fleet_skill_prefix_in_public_allowlist():
    """The /fleet/skill paths are pinned in the middleware EXEMPT_PATHS allow-list."""
    from app.middleware.api_key import APIKeyMiddleware

    assert "/fleet/skill" in APIKeyMiddleware.EXEMPT_PATHS
    assert "/fleet/SKILL.md" in APIKeyMiddleware.EXEMPT_PATHS
