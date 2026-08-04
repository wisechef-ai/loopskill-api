"""mesh_0408 T1-B\u2032 \u2014 GET /api/fleets/{id}/convergence route tests.

Thin HTTP-adapter tests over app.services.loop_convergence.fleet_convergence
(unit-tested exhaustively in tests/test_mesh0408_t1b_convergence.py). These
prove the route is wired, authed the same way as the sibling dashboard route,
and returns the per-assignment shape.
"""

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

    u = User(email="c@t.com", display_name="C", subscription_tier="pro")
    db.add(u)
    db.flush()
    pt, pfx, hs = _generate_key()
    k = APIKey(user_id=u.id, key_prefix=pfx, key_hash=hs, is_active=True)
    db.add(k)
    db.flush()
    f = Fleet(owner_user_id=u.id, name="conv-fleet", fleet_api_key_hash=hashlib.sha256(b"c").hexdigest())
    db.add(f)
    db.flush()
    db.commit()
    return u, pt, k, f


def test_convergence_empty_fleet(client, db_session):
    u, pt, k, f = _setup(db_session)
    r = client.get(f"/api/fleets/{f.id}/convergence", headers={"x-api-key": pt})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "green"
    assert body["assignment_count"] == 0
    assert body["assignments"] == []


def test_convergence_route_flags_a_stuck_loop(client, db_session):
    import uuid as _uuid

    from app.models import FleetMember, LoopPlacement, LoopRun

    u, pt, k, f = _setup(db_session)
    m = FleetMember(fleet_id=f.id, host="h", profile="d", skills_dir="~", api_key_id=k.id)
    db_session.add(m)
    db_session.flush()
    db_session.add(
        LoopPlacement(
            id=_uuid.uuid4(),
            fleet_id=f.id,
            loop_key="stuck-loop",
            member_id=m.id,
            status="active",
            placement_epoch=1,
        )
    )
    for _ in range(3):
        db_session.add(
            LoopRun(
                id=_uuid.uuid4(),
                member_id=m.id,
                fleet_id=f.id,
                loop_slug="stuck-loop",
                instance_key=_uuid.uuid4().hex,
                outcome="failure",
                placement_epoch=1,
            )
        )
    db_session.commit()

    r = client.get(f"/api/fleets/{f.id}/convergence", headers={"x-api-key": pt})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "red"
    assert body["failing_count"] == 1
    assert body["assignments"][0]["state"] == "failing"
    assert body["assignments"][0]["consecutive_failures"] == 3


def test_convergence_non_owner_404(client, db_session):
    from app.api_key_routes import _generate_key
    from app.models import APIKey, User

    u, pt, k, f = _setup(db_session)
    other = User(email="y@t.com", display_name="Y", subscription_tier="pro")
    db_session.add(other)
    db_session.flush()
    pt2, pfx2, hs2 = _generate_key()
    db_session.add(APIKey(user_id=other.id, key_prefix=pfx2, key_hash=hs2, is_active=True))
    db_session.commit()
    r = client.get(f"/api/fleets/{f.id}/convergence", headers={"x-api-key": pt2})
    assert r.status_code == 404  # no existence leak, mirrors the dashboard route
