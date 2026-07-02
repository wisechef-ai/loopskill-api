"""activate_0701 Phase TEN — TENANCY + TIER KEY CAPS.

TDD RED-first tests per docs/design/activate0701-phaseT-tenancy-keycaps.md §Tests.

Tests (contract §Tests, 1-indexed):
  1. Cross-org isolation: user A's fleet invisible to user B (404, not 403)
  2. Org-scoped bundle subscribe: fleet in org A cannot subscribe org B's bundle
  3. Key cap free: 1st member OK, 2nd -> 402 structured error
  4. Key cap pro: 200th OK, 201st -> 402
  5. Org create: POST /api/orgs -> 201, membership role='owner'
  6. Org member enroll: owner adds member; member can see org fleets
  7. Evergreen regression: org_id=NULL personal scope unchanged
  8. AuthContext org resolution: middleware stamps org_id from membership
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
        display_name="tenancy-user",
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


def _mk_fleet(db, owner, *, org_id=None):
    from app.models import Fleet

    fleet = Fleet(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        name="tenancy-fleet",
        fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
        org_id=org_id,
    )
    db.add(fleet)
    db.flush()
    return fleet


def _mk_bundle(db, owner, *, org_id=None):
    from app.models import Bundle

    cb = Bundle(
        id=uuid.uuid4(),
        name="tenancy-bundle",
        bundle_owner=owner.id,
        org_id=org_id,
    )
    db.add(cb)
    db.flush()
    return cb


def _mk_org(db, *, name="test-org"):
    from app.models import Org

    org = Org(
        id=uuid.uuid4(),
        name=name,
        slug=f"{name}-{uuid.uuid4().hex[:6]}",
        api_key_hash="",
    )
    db.add(org)
    db.flush()
    return org


def _mk_org_membership(db, org, user, *, role="owner"):
    from app.models import OrgMembership

    m = OrgMembership(
        id=uuid.uuid4(),
        org_id=org.id,
        user_id=user.id,
        role=role,
    )
    db.add(m)
    db.flush()
    return m


def _enroll(client, fleet_id, key, *, host="h", profile="default"):
    """Helper: POST enroll a member. Returns the response."""
    return client.post(
        f"/api/fleets/{fleet_id}/members",
        headers={"x-api-key": key},
        json={"host": host, "profile": profile, "skills_dir": "~/.hermes/loopskill"},
    )


# ── 1. Cross-org isolation (404, not 403 — no existence leak) ─────────────


def test_cross_org_isolation_fleet_404(middleware_client, db_session):
    """User A's org fleet is invisible to user B (404, no existence leak)."""
    org_a = _mk_org(db_session, name="org-a")
    owner_a = _mk_user(db_session)
    _mk_org_membership(db_session, org_a, owner_a)
    fleet_a = _mk_fleet(db_session, owner_a, org_id=org_a.id)
    db_session.commit()

    # User B in org B
    org_b = _mk_org(db_session, name="org-b")
    owner_b = _mk_user(db_session)
    _mk_org_membership(db_session, org_b, owner_b)
    key_b = _mk_key(db_session, owner_b)
    db_session.commit()

    # User B tries to list members of org A's fleet → 404
    r = middleware_client.get(
        f"/api/fleets/{fleet_a.id}/members",
        headers={"x-api-key": key_b},
    )
    assert r.status_code == 404, r.text

    # User B tries to enroll in org A's fleet → 404
    r2 = _enroll(middleware_client, fleet_a.id, key_b, host="b-host")
    assert r2.status_code == 404, r2.text


# ── 2. Org-scoped bundle subscribe ────────────────────────────────────────


def test_org_bundle_subscribe_boundary(middleware_client, db_session):
    """Fleet in org A cannot subscribe to org B's private bundle."""
    org_a = _mk_org(db_session, name="org-a-sub")
    owner_a = _mk_user(db_session)
    _mk_org_membership(db_session, org_a, owner_a)
    key_a = _mk_key(db_session, owner_a)
    fleet_a = _mk_fleet(db_session, owner_a, org_id=org_a.id)

    org_b = _mk_org(db_session, name="org-b-sub")
    owner_b = _mk_user(db_session)
    _mk_org_membership(db_session, org_b, owner_b)
    bundle_b = _mk_bundle(db_session, owner_b, org_id=org_b.id)
    db_session.commit()

    # Fleet A tries to subscribe to bundle B → forbidden
    r = middleware_client.post(
        f"/api/fleets/{fleet_a.id}/subscribe",
        headers={"x-api-key": key_a},
        json={"cookbook_id": str(bundle_b.id), "channel": "stable"},
    )
    assert r.status_code in (403, 404), r.text

    # Same-org bundle subscription should work
    bundle_a = _mk_bundle(db_session, owner_a, org_id=org_a.id)
    db_session.commit()
    r_ok = middleware_client.post(
        f"/api/fleets/{fleet_a.id}/subscribe",
        headers={"x-api-key": key_a},
        json={"cookbook_id": str(bundle_a.id), "channel": "stable"},
    )
    assert r_ok.status_code == 201, r_ok.text


# ── 3. Key cap free: 1st OK, 2nd → 402 ────────────────────────────────────


def test_key_cap_free_1st_ok_2nd_402(middleware_client, db_session):
    """Free tier: 1st member key OK, 2nd → 402 structured error."""
    owner = _mk_user(db_session, tier="free")
    key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner, org_id=None)
    db_session.commit()

    # 1st enrollment → 201 (cap=1, current=0)
    r1 = _enroll(middleware_client, fleet.id, key, host="free-1")
    assert r1.status_code == 201, r1.text

    # 2nd enrollment → 402
    r2 = _enroll(middleware_client, fleet.id, key, host="free-2")
    assert r2.status_code == 402, r2.text
    body = r2.json()["detail"]
    assert body["error"] == "tier_key_cap_exceeded"
    assert body["tier"] == "free"
    assert body["cap"] == 1
    assert body["current"] == 1
    assert body["upgrade_url"] == "/pricing"


# ── 4. Key cap pro: 200th OK, 201st → 402 ─────────────────────────────────


def test_key_cap_pro_200th_ok_201st_402(middleware_client, db_session):
    """Pro tier: 200th member OK, 201st → 402."""
    from app.models import APIKey, FleetMember

    owner = _mk_user(db_session, tier="pro")
    key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner, org_id=None)

    # Pre-populate 199 active members directly in DB
    for i in range(199):
        k = APIKey(
            id=uuid.uuid4(),
            user_id=owner.id,
            key_prefix=f"bulk{i}",
            key_hash=hashlib.sha256(f"rec_live_bulk_{i}_{uuid.uuid4().hex}".encode()).hexdigest(),
            name=f"bulk-{i}",
            is_active=True,
            is_test=True,
        )
        db_session.add(k)
        db_session.add(
            FleetMember(
                id=uuid.uuid4(),
                fleet_id=fleet.id,
                host=f"bulk-host-{i}",
                profile="default",
                skills_dir="~/x",
                api_key_id=k.id,
                is_active=True,
            )
        )
    db_session.commit()

    # 200th enrollment → 201 (cap=200, current=199)
    r200 = _enroll(middleware_client, fleet.id, key, host="pro-200")
    assert r200.status_code == 201, r200.text

    # 201st enrollment → 402
    r201 = _enroll(middleware_client, fleet.id, key, host="pro-201")
    assert r201.status_code == 402, r201.text
    body = r201.json()["detail"]
    assert body["error"] == "tier_key_cap_exceeded"
    assert body["tier"] == "pro"
    assert body["cap"] == 200
    assert body["current"] == 200
    assert body["upgrade_url"] == "/pricing"


# ── 5. Org create: POST /api/orgs → 201 ───────────────────────────────────


def test_org_create_201(middleware_client, db_session):
    """POST /api/orgs creates Org + OrgMembership(role='owner')."""
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    db_session.commit()

    r = middleware_client.post(
        "/api/orgs",
        headers={"x-api-key": key},
        json={"name": "My Agency"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "My Agency"
    assert "org_id" in body
    assert "slug" in body
    assert body["role"] == "owner"

    # Verify OrgMembership was created
    from app.models import OrgMembership

    org_uuid = uuid.UUID(body["org_id"])
    membership = (
        db_session.query(OrgMembership)
        .filter(OrgMembership.org_id == org_uuid, OrgMembership.user_id == owner.id)
        .first()
    )
    assert membership is not None
    assert membership.role == "owner"


# ── 6. Org member enroll: owner adds a member ─────────────────────────────


def test_org_member_enroll(middleware_client, db_session):
    """Org owner adds a member; member can see org fleets."""
    org = _mk_org(db_session, name="member-test-org")
    owner = _mk_user(db_session)
    _mk_org_membership(db_session, org, owner, role="owner")
    key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner, org_id=org.id)

    # Another user (not yet in org)
    member = _mk_user(db_session, email_override=None) if False else _mk_user(db_session)
    db_session.commit()

    # Owner adds member to org
    r = middleware_client.post(
        f"/api/orgs/{org.id}/members",
        headers={"x-api-key": key},
        json={"user_id": str(member.id), "role": "member"},
    )
    assert r.status_code == 201, r.text

    # Member can now see org fleets
    member_key = _mk_key(db_session, member)
    db_session.commit()

    r_list = middleware_client.get(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": member_key},
    )
    assert r_list.status_code == 200, r_list.text


# ── 7. Evergreen regression: org_id=NULL personal scope unchanged ──────────


def test_evergreen_personal_scope_unchanged(middleware_client, db_session):
    """Existing personal-scope fleets (org_id=NULL) still work as before."""
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner, org_id=None)
    db_session.commit()

    # Owner can enroll (pro tier → cap 200, should be fine for 1)
    r = _enroll(middleware_client, fleet.id, key, host="evergreen-host")
    assert r.status_code == 201, r.text

    # Owner can list
    r_list = middleware_client.get(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": key},
    )
    assert r_list.status_code == 200, r_list.text

    # Non-owner (no org) → 404 (existing authz unchanged)
    other = _mk_user(db_session)
    other_key = _mk_key(db_session, other)
    db_session.commit()
    r_other = _enroll(middleware_client, fleet.id, other_key, host="other-host")
    assert r_other.status_code == 404, r_other.text


# ── 8. AuthContext org resolution via middleware ──────────────────────────


def test_middleware_org_resolution(middleware_client, db_session):
    """Middleware stamps org_id on auth_ctx from OrgMembership."""
    org = _mk_org(db_session, name="middleware-test-org")
    owner = _mk_user(db_session)
    _mk_org_membership(db_session, org, owner, role="owner")
    key = _mk_key(db_session, owner)
    db_session.commit()

    # Create a fleet via API — it should inherit the caller's org_id
    r = middleware_client.post(
        "/api/fleets",
        headers={"x-api-key": key},
        json={"name": "org-fleet-via-api"},
    )
    assert r.status_code == 201, r.text
    fleet_id = r.json()["fleet_id"]

    from app.models import Fleet

    fleet = db_session.query(Fleet).filter(Fleet.id == uuid.UUID(fleet_id)).first()
    assert fleet is not None
    assert fleet.org_id == org.id, f"Fleet created by org member should inherit org_id, got {fleet.org_id}"
