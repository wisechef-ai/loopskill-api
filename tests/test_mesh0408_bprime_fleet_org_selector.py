"""mesh_0408 decision B' — optional org_id selector on POST /api/fleets.

Adam is a Forward Deployed Engineer with ONE user account that belongs to
MULTIPLE orgs (his own 'WiseChef AI' + client orgs like 'Astrovita').
OrgMembership is already unique on (org_id, user_id), not on user_id — so a
user can already belong to N orgs. The ONLY gap was that fleet creation had
no way to pick which org: `_resolve_org_membership` always resolved the
CALLER'S OLDEST membership, so a fleet meant for org B silently landed in
org A. This test file pins the fix: an optional, membership-validated
`org_id` on POST /api/fleets.

SECURITY DISTINCTION (see PR body for the full argument): this is NOT T0-B
(the sprint-opening defect of trusting an asserted tenant string with no
check). This is SELECTION — the request narrows an already-server-verified
set of orgs (OrgMembership rows), it never grants membership in anything new.

Tests:
  1. org_id omitted → unchanged behaviour: fleet lands in oldest membership.
  2. org_id provided (2nd, non-oldest org) → fleet lands in the requested org.
  3. org_id for an org the caller is NOT a member of → 403, no Fleet row created.
  4. malformed org_id → 422.
  5. THE KEY TEST — a fleet created in org B is not usable/visible to a
     caller scoped to org A, reusing authz.can_use_fleet / can_read_cookbook-
     style helpers (can_use_fleet) rather than reimplementing the check.
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
        display_name="bprime-user",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user, *, label="bprime-key"):
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


def _mk_org(db, *, name="bprime-org"):
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


def _mk_org_membership(db, org, user, *, role="owner", created_at=None):
    from app.models import OrgMembership

    kwargs = dict(id=uuid.uuid4(), org_id=org.id, user_id=user.id, role=role)
    if created_at is not None:
        kwargs["created_at"] = created_at
    m = OrgMembership(**kwargs)
    db.add(m)
    db.flush()
    return m


# ── 1. org_id omitted → unchanged: fleet lands in oldest membership ───────


def test_omit_org_id_preserves_oldest_membership_behaviour(middleware_client, db_session):
    """No org_id in the body → identical to pre-existing behaviour: the
    fleet inherits ctx.org_id, which _resolve_org_membership resolves to the
    caller's OLDEST OrgMembership row."""
    import datetime

    user = _mk_user(db_session)
    org_old = _mk_org(db_session, name="oldest-org")
    org_new = _mk_org(db_session, name="newer-org")
    # Oldest membership first (earlier created_at) — must win when org_id omitted.
    _mk_org_membership(
        db_session, org_old, user, created_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    )
    _mk_org_membership(
        db_session, org_new, user, created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    )
    key = _mk_key(db_session, user)
    db_session.commit()

    r = middleware_client.post("/api/fleets", headers={"x-api-key": key}, json={"name": "no-org-id-fleet"})
    assert r.status_code == 201, r.text
    fleet_id = r.json()["fleet_id"]

    from app.models import Fleet

    fleet = db_session.query(Fleet).filter(Fleet.id == uuid.UUID(fleet_id)).first()
    assert fleet is not None
    assert fleet.org_id == org_old.id, (
        f"omitting org_id must preserve today's oldest-membership behaviour, "
        f"got org_id={fleet.org_id}, expected oldest={org_old.id}"
    )


# ── 2. org_id provided (2nd, non-oldest org) → lands in requested org ─────


def test_org_id_selects_second_non_oldest_org(middleware_client, db_session):
    """A user in TWO orgs can create a fleet in the SECOND (non-oldest) org
    by passing org_id — Fleet.org_id must equal the REQUESTED org, not the
    oldest one that _resolve_org_membership would otherwise pick."""
    import datetime

    user = _mk_user(db_session)
    org_old = _mk_org(db_session, name="wisechef-ai")
    org_new = _mk_org(db_session, name="astrovita")
    _mk_org_membership(
        db_session, org_old, user, created_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    )
    _mk_org_membership(
        db_session, org_new, user, created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    )
    key = _mk_key(db_session, user)
    db_session.commit()

    r = middleware_client.post(
        "/api/fleets",
        headers={"x-api-key": key},
        json={"name": "astrovita-fleet", "org_id": str(org_new.id)},
    )
    assert r.status_code == 201, r.text
    fleet_id = r.json()["fleet_id"]

    from app.models import Fleet

    fleet = db_session.query(Fleet).filter(Fleet.id == uuid.UUID(fleet_id)).first()
    assert fleet is not None
    assert fleet.org_id == org_new.id, (
        f"org_id selector must land the fleet in the REQUESTED org, "
        f"not the oldest membership: got {fleet.org_id}, expected {org_new.id}"
    )
    assert fleet.org_id != org_old.id


# ── 3. org_id for a NON-member org → 403, no Fleet row created ────────────


def test_org_id_for_non_member_org_is_forbidden_and_creates_nothing(middleware_client, db_session):
    """Passing an org the caller is NOT a member of → 403, and no Fleet row
    is created as a side effect (assert the count is unchanged)."""
    from app.models import Fleet

    user = _mk_user(db_session)
    own_org = _mk_org(db_session, name="own-org")
    _mk_org_membership(db_session, own_org, user)
    other_org = _mk_org(db_session, name="not-my-org")
    # deliberately NO membership for `user` in other_org
    key = _mk_key(db_session, user)
    db_session.commit()

    before_count = db_session.query(Fleet).count()

    r = middleware_client.post(
        "/api/fleets",
        headers={"x-api-key": key},
        json={"name": "should-not-exist", "org_id": str(other_org.id)},
    )
    assert r.status_code == 403, r.text

    after_count = db_session.query(Fleet).count()
    assert after_count == before_count, (
        f"forbidden org_id must create NO Fleet row: before={before_count}, after={after_count}"
    )


# ── 4. malformed org_id → 422 ──────────────────────────────────────────────


def test_malformed_org_id_returns_422(middleware_client, db_session):
    user = _mk_user(db_session)
    org = _mk_org(db_session, name="malformed-test-org")
    _mk_org_membership(db_session, org, user)
    key = _mk_key(db_session, user)
    db_session.commit()

    r = middleware_client.post(
        "/api/fleets",
        headers={"x-api-key": key},
        json={"name": "bad-org-id-fleet", "org_id": "not-a-uuid"},
    )
    assert r.status_code == 422, r.text


# ── 5. THE KEY TEST — cross-org isolation via authz.can_use_fleet ─────────


def test_fleet_in_org_b_not_usable_by_caller_scoped_to_org_a(middleware_client, db_session):
    """A fleet created (via org_id selector) in org B must NOT be usable by
    a DIFFERENT caller scoped to org A — reusing authz.can_use_fleet (the
    same predicate the fleet routes/authz layer already enforces) rather
    than reimplementing the check inline. This proves the org_id selector
    does not weaken the existing tenant boundary: it only lets the OWNING
    caller pick among THEIR OWN orgs, never grants cross-org access."""
    from app.auth_ctx import AuthContext
    from app import authz
    from app.models import Fleet

    fde_user = _mk_user(db_session)  # the FDE — belongs to both orgs
    org_a = _mk_org(db_session, name="org-a-isolation")
    org_b = _mk_org(db_session, name="org-b-isolation")
    _mk_org_membership(db_session, org_a, fde_user)
    _mk_org_membership(db_session, org_b, fde_user)
    fde_key = _mk_key(db_session, fde_user)
    db_session.commit()

    # FDE creates a fleet explicitly in org B.
    r = middleware_client.post(
        "/api/fleets",
        headers={"x-api-key": fde_key},
        json={"name": "org-b-only-fleet", "org_id": str(org_b.id)},
    )
    assert r.status_code == 201, r.text
    fleet_b = db_session.query(Fleet).filter(Fleet.id == uuid.UUID(r.json()["fleet_id"])).first()
    assert fleet_b is not None
    assert fleet_b.org_id == org_b.id

    # A DIFFERENT caller, scoped only to org A, must not be able to use it.
    other_user = _mk_user(db_session)
    _mk_org_membership(db_session, org_a, other_user)
    db_session.commit()

    other_ctx = AuthContext(scope="user", user_id=other_user.id, org_id=org_a.id)
    assert authz.can_use_fleet(other_ctx, fleet_b) is False, (
        "a caller scoped to org A must not be authorized to use a fleet "
        "created in org B via the org_id selector"
    )

    # Sanity: the FDE, scoped to org B, CAN use it (their own membership).
    fde_ctx_b = AuthContext(scope="user", user_id=fde_user.id, org_id=org_b.id)
    assert authz.can_use_fleet(fde_ctx_b, fleet_b) is True

    # Also confirm via the live route surface: org-A-scoped caller's list
    # of fleets does not include the org-B fleet (end-to-end, not just the
    # pure predicate).
    other_key = _mk_key(db_session, other_user)
    db_session.commit()
    list_resp = middleware_client.get("/api/fleets", headers={"x-api-key": other_key})
    assert list_resp.status_code == 200
    seen_ids = {f["fleet_id"] for f in list_resp.json()["fleets"]}
    assert str(fleet_b.id) not in seen_ids
