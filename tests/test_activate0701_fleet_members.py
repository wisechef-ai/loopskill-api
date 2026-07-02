"""Phase 1 (loopskill_activate_0701) — FLEET-UNIT + AGENT KEYS.

TDD RED-first tests per docs/design/activate0701-phase1-fleet-member.md §5.
Product lock #13: the per-agent API key IS the member identity. One key =
one agent = one FleetMember. ReconcileEvent must carry member identity.

Tests (contract §5, 1-indexed to match the design doc):
  1. enroll happy path
  2. THE PHASE GATE — two members on one host, reconcile-report each →
     two ReconcileEvents with distinct non-null member_id
  3. duplicate (fleet,host,profile) → 409
  4. keyset pagination
  5. authz: non-owner → 404; fleet-scope ctx → 403; anonymous → 401
  6. delete idempotency
  7. reconcile poll with a member key still works (regression)
  8. non-member key reconcile-report → member_id NULL (backward compat)
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
        display_name="fleet-member-owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user, *, label="owner-key"):
    from app.models import APIKey

    raw = f"rec_live_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:12],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name=label,
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
        name="member-fleet",
        fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
    )
    db.add(fleet)
    db.flush()
    return fleet


def _mk_fleet_key(db, fleet):
    """Mint a genuine rec_fleet_ key hashed to match fleet.fleet_api_key_hash."""
    from app.models import Fleet

    raw = f"rec_fleet_{uuid.uuid4().hex[:8]}_{uuid.uuid4().hex}"
    fleet.fleet_api_key_hash = hashlib.sha256(raw.encode()).hexdigest()
    db.flush()
    return raw


def _mk_cookbook_with_skill(db, owner):
    """A Bundle owning a declared Skill, ready for reconcile-report."""
    from app.models import Bundle, BundleSkill, Skill

    skill = Skill(
        id=uuid.uuid4(),
        slug=f"member-skill-{uuid.uuid4().hex[:6]}",
        title="Member Skill",
        category="devops",
        is_public=False,
    )
    db.add(skill)
    cb = Bundle(id=uuid.uuid4(), name="member-cb", bundle_owner=owner.id)
    db.add(cb)
    db.flush()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
    db.commit()
    return skill, cb


# ── 1. enroll happy path ────────────────────────────────────────────────


def test_enroll_happy_path(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)

    r = middleware_client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": key},
        json={"host": "adam-xps", "profile": "default", "skills_dir": "~/.hermes/loopskill"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["host"] == "adam-xps"
    assert body["profile"] == "default"
    assert body["skills_dir"] == "~/.hermes/loopskill"
    assert body["fleet_id"] == str(fleet.id)
    assert body["api_key"].startswith("rec_live_")
    assert "warning" in body
    member_id = body["member_id"]
    api_key_id = body["api_key_id"]

    from app.models import APIKey, FleetMember

    member_row = db_session.query(FleetMember).filter(FleetMember.id == uuid.UUID(member_id)).first()
    assert member_row is not None
    assert member_row.fleet_id == fleet.id
    assert member_row.host == "adam-xps"
    assert member_row.profile == "default"
    assert member_row.is_active is True
    assert str(member_row.api_key_id) == api_key_id

    key_row = db_session.query(APIKey).filter(APIKey.id == uuid.UUID(api_key_id)).first()
    assert key_row is not None
    assert key_row.is_active is True
    expected_hash = hashlib.sha256(body["api_key"].encode()).hexdigest()
    assert key_row.key_hash == expected_hash


# ── 2. THE PHASE GATE ───────────────────────────────────────────────────


def test_phase_gate_two_members_distinct_reconcile_events(middleware_client, db_session):
    """Two members on ONE host (profiles default + worker); each reconcile-reports
    with its own key → two ReconcileEvents with DISTINCT non-null member_id."""
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    skill, cb = _mk_cookbook_with_skill(db_session, owner)

    from app.models import SkillVersion

    db_session.add(
        SkillVersion(
            id=uuid.uuid4(),
            skill_id=skill.id,
            semver="1.0.0",
            tarball_path="/tmp/x/1.0.0.tar.gz",
            checksum_sha256="a" * 64,
        )
    )
    db_session.commit()

    r1 = middleware_client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": owner_key},
        json={"host": "shared-host", "profile": "default", "skills_dir": "~/.hermes/loopskill"},
    )
    r2 = middleware_client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": owner_key},
        json={"host": "shared-host", "profile": "worker", "skills_dir": "~/.hermes/loopskill"},
    )
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text

    member1_id = r1.json()["member_id"]
    member1_key = r1.json()["api_key"]
    member2_id = r2.json()["member_id"]
    member2_key = r2.json()["api_key"]
    assert member1_id != member2_id
    assert member1_key != member2_key

    resp1 = middleware_client.post(
        f"/api/bundles/{cb.id}/reconcile-report",
        headers={"x-api-key": member1_key},
        json={"slug": skill.slug, "semver": "1.0.0", "outcome": "success", "channel": "canary"},
    )
    resp2 = middleware_client.post(
        f"/api/bundles/{cb.id}/reconcile-report",
        headers={"x-api-key": member2_key},
        json={"slug": skill.slug, "semver": "1.0.0", "outcome": "success", "channel": "canary"},
    )
    assert resp1.status_code == 200, resp1.text
    assert resp2.status_code == 200, resp2.text

    from app.models import ReconcileEvent

    events = (
        db_session.query(ReconcileEvent)
        .filter(ReconcileEvent.skill_id == skill.id, ReconcileEvent.semver == "1.0.0")
        .order_by(ReconcileEvent.created_at.asc())
        .all()
    )
    assert len(events) == 2
    member_ids = {str(e.member_id) for e in events}
    assert None not in [e.member_id for e in events]
    assert member_ids == {member1_id, member2_id}


# ── 3. duplicate (fleet, host, profile) → 409 ──────────────────────────


def test_enroll_duplicate_conflict_409(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)

    body = {"host": "dup-host", "profile": "default", "skills_dir": "~/x"}
    r1 = middleware_client.post(f"/api/fleets/{fleet.id}/members", headers={"x-api-key": key}, json=body)
    assert r1.status_code == 201, r1.text

    r2 = middleware_client.post(f"/api/fleets/{fleet.id}/members", headers={"x-api-key": key}, json=body)
    assert r2.status_code == 409
    assert r2.json()["detail"] == "member_exists"


# ── 4. keyset pagination ────────────────────────────────────────────────


def test_list_members_keyset_pagination(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)

    for i in range(3):
        r = middleware_client.post(
            f"/api/fleets/{fleet.id}/members",
            headers={"x-api-key": key},
            json={"host": f"host-{i}", "profile": "default", "skills_dir": "~/x"},
        )
        assert r.status_code == 201, r.text

    page1 = middleware_client.get(
        f"/api/fleets/{fleet.id}/members", headers={"x-api-key": key}, params={"limit": 2}
    )
    assert page1.status_code == 200, page1.text
    body1 = page1.json()
    assert len(body1["members"]) == 2
    assert body1["next_after"] is not None

    page2 = middleware_client.get(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": key},
        params={"limit": 2, "after": body1["next_after"]},
    )
    assert page2.status_code == 200, page2.text
    body2 = page2.json()
    assert len(body2["members"]) == 1
    assert body2["next_after"] is None

    seen_ids = {m["member_id"] for m in body1["members"]} | {m["member_id"] for m in body2["members"]}
    assert len(seen_ids) == 3


# ── 5. authz ─────────────────────────────────────────────────────────────


def test_authz_non_owner_404(middleware_client, db_session):
    owner = _mk_user(db_session)
    fleet = _mk_fleet(db_session, owner)
    other = _mk_user(db_session)
    other_key = _mk_key(db_session, other)

    r = middleware_client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": other_key},
        json={"host": "h", "profile": "default", "skills_dir": "~/x"},
    )
    assert r.status_code == 404


def test_authz_fleet_scope_key_403(middleware_client, db_session):
    owner = _mk_user(db_session)
    fleet = _mk_fleet(db_session, owner)
    fleet_key = _mk_fleet_key(db_session, fleet)

    r = middleware_client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": fleet_key},
        json={"host": "h", "profile": "default", "skills_dir": "~/x"},
    )
    assert r.status_code == 403


def test_authz_anonymous_401(middleware_client, db_session):
    owner = _mk_user(db_session)
    fleet = _mk_fleet(db_session, owner)

    r = middleware_client.post(
        f"/api/fleets/{fleet.id}/members",
        json={"host": "h", "profile": "default", "skills_dir": "~/x"},
    )
    assert r.status_code == 401


# ── 6. delete idempotency ───────────────────────────────────────────────


def test_delete_idempotent_and_revokes_key(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)

    enroll = middleware_client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": key},
        json={"host": "del-host", "profile": "default", "skills_dir": "~/x"},
    )
    member_id = enroll.json()["member_id"]
    api_key_id = enroll.json()["api_key_id"]

    d1 = middleware_client.delete(f"/api/fleets/{fleet.id}/members/{member_id}", headers={"x-api-key": key})
    assert d1.status_code == 200, d1.text
    assert d1.json() == {"removed": True, "member_id": member_id}

    d2 = middleware_client.delete(f"/api/fleets/{fleet.id}/members/{member_id}", headers={"x-api-key": key})
    assert d2.status_code == 200
    assert d2.json() == {"removed": True, "member_id": member_id}

    from app.models import APIKey, FleetMember

    member_row = db_session.query(FleetMember).filter(FleetMember.id == uuid.UUID(member_id)).first()
    assert member_row.is_active is False
    key_row = db_session.query(APIKey).filter(APIKey.id == uuid.UUID(api_key_id)).first()
    assert key_row.is_active is False

    # excluded from default (active-only) list
    listing = middleware_client.get(f"/api/fleets/{fleet.id}/members", headers={"x-api-key": key})
    ids = {m["member_id"] for m in listing.json()["members"]}
    assert member_id not in ids


# ── 7. reconcile poll with a member key still works (regression) ───────


def test_member_key_reconcile_report_regression(middleware_client, db_session):
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    skill, cb = _mk_cookbook_with_skill(db_session, owner)

    from app.models import SkillVersion

    db_session.add(
        SkillVersion(
            id=uuid.uuid4(),
            skill_id=skill.id,
            semver="1.0.0",
            tarball_path="/tmp/y/1.0.0.tar.gz",
            checksum_sha256="b" * 64,
        )
    )
    db_session.commit()

    enroll = middleware_client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": owner_key},
        json={"host": "regress-host", "profile": "default", "skills_dir": "~/x"},
    )
    assert enroll.status_code == 201, enroll.text
    member_key = enroll.json()["api_key"]

    resp = middleware_client.post(
        f"/api/bundles/{cb.id}/reconcile-report",
        headers={"x-api-key": member_key},
        json={"slug": skill.slug, "semver": "1.0.0", "outcome": "success", "channel": "canary"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["recorded"] is True


# ── 8. non-member key reconcile-report → member_id NULL (backward compat) ─


def test_non_member_key_reconcile_report_member_id_null(middleware_client, db_session):
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    skill, cb = _mk_cookbook_with_skill(db_session, owner)

    from app.models import SkillVersion

    db_session.add(
        SkillVersion(
            id=uuid.uuid4(),
            skill_id=skill.id,
            semver="1.0.0",
            tarball_path="/tmp/z/1.0.0.tar.gz",
            checksum_sha256="c" * 64,
        )
    )
    db_session.commit()

    resp = middleware_client.post(
        f"/api/bundles/{cb.id}/reconcile-report",
        headers={"x-api-key": owner_key},
        json={"slug": skill.slug, "semver": "1.0.0", "outcome": "success", "channel": "canary"},
    )
    assert resp.status_code == 200, resp.text

    from app.models import ReconcileEvent

    ev = (
        db_session.query(ReconcileEvent)
        .filter(ReconcileEvent.skill_id == skill.id, ReconcileEvent.semver == "1.0.0")
        .first()
    )
    assert ev is not None
    assert ev.member_id is None
