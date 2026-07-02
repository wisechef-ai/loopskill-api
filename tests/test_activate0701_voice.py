"""Phase FB+VOICE (loopskill_activate_0701) — agent voice + fleet owner inbox tests."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from tests._app_factory import build_test_app


@pytest.fixture
def client(db_session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_user(db, email="owner@test.com", tier="pro"):
    from app.models import User

    u = User(email=email, display_name="Owner", subscription_tier=tier)
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user):
    from app.models import APIKey

    k = APIKey(user_id=user.id, key_prefix="rec_live_xx", key_hash=f"hash{user.id}", is_active=True)
    db.add(k)
    db.flush()
    return k


def _mk_fleet(db, user):
    import hashlib

    from app.models import Fleet

    f = Fleet(
        owner_user_id=user.id, name="test-fleet", fleet_api_key_hash=hashlib.sha256(b"test").hexdigest()
    )
    db.add(f)
    db.flush()
    return f


def _mk_member(db, fleet, user, api_key):
    from app.models import FleetMember

    m = FleetMember(
        fleet_id=fleet.id, host="test-host", profile="default", skills_dir="~/x", api_key_id=api_key.id
    )
    db.add(m)
    db.flush()
    return m


def test_voice_inbox_empty(client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    db_session.commit()

    r = client.get(f"/api/fleets/{fleet.id}/voice-inbox", headers={"x-api-key": "rec_live_test"})
    # Need a real key — use the middleware pattern
    from app.api_key_routes import _generate_key

    plaintext, prefix, hash_val = _generate_key()
    from app.models import APIKey

    real_key = APIKey(user_id=owner.id, key_prefix=prefix, key_hash=hash_val, is_active=True)
    db_session.add(real_key)
    db_session.commit()

    r = client.get(
        f"/api/fleets/{fleet.id}/voice-inbox",
        headers={"x-api-key": plaintext},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["items"] == []


def test_voice_inbox_shows_skill_error(client, db_session):
    from app.models import FleetMember, SkillErrorReport

    owner = _mk_user(db_session)
    from app.api_key_routes import _generate_key

    plaintext, prefix, hash_val = _generate_key()
    from app.models import APIKey

    key = APIKey(user_id=owner.id, key_prefix=prefix, key_hash=hash_val, is_active=True)
    db_session.add(key)
    db_session.flush()
    fleet = _mk_fleet(db_session, owner)
    member = FleetMember(
        fleet_id=fleet.id, host="h", profile="d", skills_dir="~", api_key_id=key.id, is_active=True
    )
    db_session.add(member)
    db_session.flush()
    err = SkillErrorReport(
        member_id=member.id,
        fleet_id=fleet.id,
        slug="broken-skill",
        signature="sig123",
        summary="This skill crashed",
    )
    db_session.add(err)
    db_session.commit()

    r = client.get(
        f"/api/fleets/{fleet.id}/voice-inbox",
        headers={"x-api-key": plaintext},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["type"] == "skill_error"
    assert body["items"][0]["slug"] == "broken-skill"


def test_voice_inbox_resolve(client, db_session):
    from app.models import FleetMember, SkillErrorReport

    owner = _mk_user(db_session)
    from app.api_key_routes import _generate_key

    plaintext, prefix, hash_val = _generate_key()
    from app.models import APIKey

    key = APIKey(user_id=owner.id, key_prefix=prefix, key_hash=hash_val, is_active=True)
    db_session.add(key)
    db_session.flush()
    fleet = _mk_fleet(db_session, owner)
    member = FleetMember(
        fleet_id=fleet.id, host="h", profile="d", skills_dir="~", api_key_id=key.id, is_active=True
    )
    db_session.add(member)
    db_session.flush()
    err = SkillErrorReport(
        member_id=member.id,
        fleet_id=fleet.id,
        slug="bad",
        signature="s",
        summary="err",
    )
    db_session.add(err)
    db_session.commit()
    err_id = str(err.id)

    r = client.post(
        f"/api/fleets/{fleet.id}/voice-inbox/skill_error/{err_id}/resolve",
        headers={"x-api-key": plaintext},
    )
    assert r.status_code == 200, r.text
    db_session.expire_all()
    refreshed = db_session.query(SkillErrorReport).filter(SkillErrorReport.id == err.id).first()
    assert refreshed.feedback_status == "resolved"


def test_voice_inbox_non_owner_404(client, db_session):
    from app.models import User

    owner = _mk_user(db_session)
    other = User(email="other@test.com", display_name="Other", subscription_tier="pro")
    db_session.add(other)
    db_session.flush()
    from app.api_key_routes import _generate_key

    pt1, p1, h1 = _generate_key()
    pt2, p2, h2 = _generate_key()
    from app.models import APIKey

    db_session.add(APIKey(user_id=owner.id, key_prefix=p1, key_hash=h1, is_active=True))
    db_session.add(APIKey(user_id=other.id, key_prefix=p2, key_hash=h2, is_active=True))
    fleet = _mk_fleet(db_session, owner)
    db_session.commit()

    r = client.get(
        f"/api/fleets/{fleet.id}/voice-inbox",
        headers={"x-api-key": pt2},  # other user's key
    )
    assert r.status_code == 404  # no existence leak
