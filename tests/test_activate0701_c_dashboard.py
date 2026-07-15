"""Phase C (loopskill_activate_0701) — fleet dashboard API tests."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from tests._app_factory import build_test_app


@pytest.fixture
def client(db_session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _setup(db):
    from app.api_key_routes import _generate_key
    from app.models import APIKey, Fleet, User

    u = User(email="d@t.com", display_name="D", subscription_tier="pro")
    db.add(u)
    db.flush()
    pt, pfx, hs = _generate_key()
    k = APIKey(user_id=u.id, key_prefix=pfx, key_hash=hs, is_active=True)
    db.add(k)
    db.flush()
    f = Fleet(owner_user_id=u.id, name="dash-fleet", fleet_api_key_hash=hashlib.sha256(b"d").hexdigest())
    db.add(f)
    db.flush()
    db.commit()
    return u, pt, k, f


def test_dashboard_empty(client, db_session):
    u, pt, k, f = _setup(db_session)
    r = client.get(f"/api/fleets/{f.id}/dashboard", headers={"x-api-key": pt})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fleet_name"] == "dash-fleet"
    assert body["members"] == []
    assert body["voice"]["skill_errors"] == 0


def test_dashboard_with_member(client, db_session):
    from app.models import FleetMember

    u, pt, k, f = _setup(db_session)
    m = FleetMember(fleet_id=f.id, host="h", profile="d", skills_dir="~", api_key_id=k.id)
    db_session.add(m)
    db_session.commit()
    r = client.get(f"/api/fleets/{f.id}/dashboard", headers={"x-api-key": pt})
    assert r.status_code == 200
    body = r.json()
    assert len(body["members"]) == 1
    assert body["members"][0]["host"] == "h"
    assert "failed_crons" in body["members"][0]


def test_dashboard_non_owner_404(client, db_session):
    from app.api_key_routes import _generate_key
    from app.models import APIKey, User

    u, pt, k, f = _setup(db_session)
    other = User(email="x@t.com", display_name="X", subscription_tier="pro")
    db_session.add(other)
    db_session.flush()
    pt2, pfx2, hs2 = _generate_key()
    db_session.add(APIKey(user_id=other.id, key_prefix=pfx2, key_hash=hs2, is_active=True))
    db_session.commit()
    r = client.get(f"/api/fleets/{f.id}/dashboard", headers={"x-api-key": pt2})
    assert r.status_code == 404  # no existence leak


def test_dashboard_surfaces_ralph_loops(client, db_session):
    """spotify_1507 Ph F — a stuck member (6 same-outcome no-change runs) shows
    up in the dashboard's ralph_loops so the pane can flag a spinning agent."""
    import uuid as _uuid

    from app.models import FleetMember, LoopRun

    u, pt, k, f = _setup(db_session)
    m = FleetMember(fleet_id=f.id, host="h", profile="d", skills_dir="~", api_key_id=k.id)
    db_session.add(m)
    db_session.flush()
    for _ in range(6):
        db_session.add(
            LoopRun(
                id=_uuid.uuid4(),
                member_id=m.id,
                fleet_id=f.id,
                loop_slug="stuck-loop",
                instance_key="i1",
                outcome="no_change",
                accepted_change=False,
            )
        )
    db_session.commit()

    r = client.get(f"/api/fleets/{f.id}/dashboard", headers={"x-api-key": pt})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ralph_count"] == 1
    assert body["ralph_loops"][0]["loop_slug"] == "stuck-loop"
    assert body["ralph_loops"][0]["run_count"] == 6
