"""feat/fleet-console-state — lockfile snapshot persistence + console reads.

The fleet console contract:
  1. sync-report lockfile_state PERSISTS (one upserted row per member).
  2. GET /fleets/{id}/members/{mid}/state → installed / missing / extras.
  3. GET /fleets/{id}/inventory → whole-fleet drift matrix.
  4. Extras (installed-but-undeclared) surface as harvest candidates.
  5. Non-owner → 404 (no existence leak); snapshot-less member degrades clean.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_user(db, *, tier="pro"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="console-owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user):
    from app.models import APIKey

    raw = f"rec_live_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:12],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name="console-key",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _mk_fleet(db, owner):
    from app.models import Fleet

    fleet = Fleet(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        name="console-fleet",
        fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
    )
    db.add(fleet)
    db.flush()
    return fleet


def _mk_bundle_with_skills(db, owner, fleet, slugs):
    from app.models import Bundle, BundleSkill, FleetSubscription, Skill

    cb = Bundle(id=uuid.uuid4(), name="console-bundle", bundle_owner=owner.id)
    db.add(cb)
    db.flush()
    for slug in slugs:
        s = Skill(id=uuid.uuid4(), slug=slug, title=slug, description="x", is_public=True)
        db.add(s)
        db.flush()
        db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added"))
    db.add(FleetSubscription(fleet_id=fleet.id, bundle_id=cb.id, channel="stable"))
    db.flush()
    return cb


def _enroll_member(client, fleet, owner_key, *, host="agent-host"):
    r = client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": owner_key},
        json={"host": host, "profile": "default", "skills_dir": "~/.hermes/loopskill"},
    )
    assert r.status_code == 201, r.text
    return r.json()["member_id"], r.json()["api_key"]


def _post_lockfile(client, member_key, skills, *, cycle_ts="2026-07-06T10:00:00Z"):
    r = client.post(
        "/api/sync-report",
        headers={"x-api-key": member_key},
        json={"cycle_ts": cycle_ts, "lockfile_state": skills},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _setup(client, db):
    owner = _mk_user(db)
    owner_key = _mk_key(db, owner)
    fleet = _mk_fleet(db, owner)
    _mk_bundle_with_skills(db, owner, fleet, ["declared-a", "declared-b"])
    db.commit()
    member_id, member_key = _enroll_member(client, fleet, owner_key)
    return owner, owner_key, fleet, member_id, member_key


# ── 1. persistence: snapshot lands + upserts (no row growth) ─────────────────


def test_lockfile_state_persists_and_upserts(middleware_client, db_session):
    from app.models import MemberLockfileSnapshot

    _, _, fleet, member_id, member_key = _setup(middleware_client, db_session)

    out = _post_lockfile(
        middleware_client,
        member_key,
        [{"slug": "declared-a", "pinned_version": "1.0.0", "checksum_sha256": "a" * 64}],
    )
    assert out["recorded"]["lockfile_state"] is True

    rows = db_session.query(MemberLockfileSnapshot).all()
    assert len(rows) == 1
    assert rows[0].skills[0]["slug"] == "declared-a"

    # Second report REPLACES, never appends
    _post_lockfile(
        middleware_client,
        member_key,
        [
            {"slug": "declared-a", "pinned_version": "1.1.0"},
            {"slug": "novel-skill", "pinned_version": None},
        ],
    )
    rows = db_session.query(MemberLockfileSnapshot).all()
    assert len(rows) == 1  # still one row — upsert, O(fleet) not O(time)
    slugs = {s["slug"] for s in rows[0].skills}
    assert slugs == {"declared-a", "novel-skill"}


# ── 2. member state: installed / missing / extras ────────────────────────────


def test_member_state_diff(middleware_client, db_session):
    _, owner_key, fleet, member_id, member_key = _setup(middleware_client, db_session)

    _post_lockfile(
        middleware_client,
        member_key,
        [
            {"slug": "declared-a", "pinned_version": "1.0.0"},  # in sync
            {"slug": "astrovita-new-skill", "pinned_version": None},  # extra (harvest!)
        ],
    )

    r = middleware_client.get(
        f"/api/fleets/{fleet.id}/members/{member_id}/state",
        headers={"x-api-key": owner_key},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_snapshot"] is True
    assert [s["slug"] for s in body["in_sync"]] == ["declared-a"]
    assert [s["slug"] for s in body["missing"]] == ["declared-b"]
    assert [s["slug"] for s in body["extras"]] == ["astrovita-new-skill"]
    assert body["in_sync"][0]["bundle_name"] == "console-bundle"
    assert body["declared_count"] == 2


def test_member_state_without_snapshot_degrades_clean(middleware_client, db_session):
    _, owner_key, fleet, member_id, _ = _setup(middleware_client, db_session)

    r = middleware_client.get(
        f"/api/fleets/{fleet.id}/members/{member_id}/state",
        headers={"x-api-key": owner_key},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_snapshot"] is False
    assert body["extras"] == []
    # everything declared shows as missing (agent never reported)
    assert {m["slug"] for m in body["missing"]} == {"declared-a", "declared-b"}


# ── 3. fleet inventory matrix ─────────────────────────────────────────────────


def test_fleet_inventory_matrix(middleware_client, db_session):
    _, owner_key, fleet, m1_id, m1_key = _setup(middleware_client, db_session)
    m2_id, m2_key = _enroll_member(middleware_client, fleet, owner_key, host="second-host")

    _post_lockfile(
        middleware_client,
        m1_key,
        [{"slug": "declared-a"}, {"slug": "declared-b"}, {"slug": "harvest-me"}],
    )
    _post_lockfile(middleware_client, m2_key, [{"slug": "declared-a"}])

    r = middleware_client.get(f"/api/fleets/{fleet.id}/inventory", headers={"x-api-key": owner_key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["declared_count"] == 2
    by_host = {m["host"]: m for m in body["members"]}
    assert by_host["agent-host"]["in_sync_count"] == 2
    assert by_host["agent-host"]["missing_count"] == 0
    assert by_host["agent-host"]["extras_count"] == 1
    assert by_host["agent-host"]["extras"][0]["slug"] == "harvest-me"
    assert by_host["second-host"]["in_sync_count"] == 1
    assert by_host["second-host"]["missing_count"] == 1


# ── 4. authz: non-owner 404, no existence leak ────────────────────────────────


def test_console_routes_404_for_non_owner(middleware_client, db_session):
    _, _, fleet, member_id, _ = _setup(middleware_client, db_session)
    intruder = _mk_user(db_session)
    intruder_key = _mk_key(db_session, intruder)
    db_session.commit()

    r1 = middleware_client.get(f"/api/fleets/{fleet.id}/inventory", headers={"x-api-key": intruder_key})
    r2 = middleware_client.get(
        f"/api/fleets/{fleet.id}/members/{member_id}/state",
        headers={"x-api-key": intruder_key},
    )
    assert r1.status_code == 404
    assert r2.status_code == 404
