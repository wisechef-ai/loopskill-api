"""portal_0610 J6 — cookbook living-object signals on GET /api/cookbooks/{id}.

The cookbook detail response now carries a `signals` block so the page can show
the cookbook is alive (reach + heartbeat + team usage + feedback), not a static
list. All organic-only + best-effort.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    return TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))


def _mk_user(db, *, tier="pro"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="cb-owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user):
    from app.models import APIKey

    raw = f"rec_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:8],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name="j6",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _mk_cookbook(db, owner, *, visibility="private"):
    from app.models import Cookbook

    cb = Cookbook(id=uuid.uuid4(), name="deck", cookbook_owner=owner.id, visibility=visibility)
    db.add(cb)
    db.flush()
    return cb


def _add_skill(db, cb, slug):
    from app.models import Skill, CookbookSkill

    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        description="t",
        tier="free",
        is_public=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    db.add(CookbookSkill(cookbook_id=cb.id, skill_id=sk.id, source="custom-added"))
    db.flush()
    return sk


def test_signals_block_present_and_shaped(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    _add_skill(db_session, cb, "j6-a")
    _add_skill(db_session, cb, "j6-b")

    r = middleware_client.get(f"/api/cookbooks/{cb.id}", headers={"x-api-key": key})
    assert r.status_code == 200, r.text
    sig = r.json().get("signals")
    assert sig is not None, "signals block must be present"
    # All J6 keys present
    for k in (
        "installs_total",
        "installs_7d",
        "last_synced",
        "fleet_usage",
        "skill_count",
        "corrections_absorbed",
    ):
        assert k in sig, f"missing signal {k}"
    # New cookbook, no installs → 0 (organic, honest)
    assert sig["installs_total"] == 0
    assert sig["installs_7d"] == 0
    assert sig["skill_count"] == 2
    assert sig["fleet_usage"] == 0


def test_fleet_usage_counts_subscriptions(middleware_client, db_session):
    from app.models import Fleet, FleetSubscription

    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    _add_skill(db_session, cb, "j6-fleeted")

    fleet = Fleet(id=uuid.uuid4(), owner_user_id=owner.id, name="f", fleet_api_key_hash="x" * 64)
    db_session.add(fleet)
    db_session.flush()
    db_session.add(FleetSubscription(fleet_id=fleet.id, cookbook_id=cb.id, channel="stable"))
    db_session.flush()

    r = middleware_client.get(f"/api/cookbooks/{cb.id}", headers={"x-api-key": key})
    assert r.status_code == 200
    assert r.json()["signals"]["fleet_usage"] == 1


def test_signals_skill_list_still_load_bearing(middleware_client, db_session):
    """Signals are decorative — the skills array must remain intact alongside."""
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    _add_skill(db_session, cb, "j6-core")

    body = middleware_client.get(f"/api/cookbooks/{cb.id}", headers={"x-api-key": key}).json()
    assert "skills" in body and len(body["skills"]) == 1
    assert body["skills"][0]["slug"] == "j6-core"
    assert "signals" in body
