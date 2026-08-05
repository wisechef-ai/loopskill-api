"""mesh_0408 — bundles are born tenantless: add the org selector B' gave fleets.

Live defect found 2026-08-05 during T2-B: POST /api/bundles (mounted as
POST /api/cookbooks) never wrote org_id — the ``Bundle(...)`` constructor set
id/name/description/is_base/bundle_owner and nothing else, so every bundle was
born with org_id=NULL, permanently. The org-scoped read filter in
list_cookbooks (``Bundle.org_id == ctx.org_id``) and authz.can_access_bundle's
org-match branch were consequently DEAD CODE in practice — no bundle created
through this route could ever match them.

This is the SAME defect class B' (PR #191, 04ed26c) fixed for fleets. This
file mirrors that fix and that test file
(tests/test_mesh0408_bprime_fleet_org_selector.py) exactly, adapted to
bundles:

  1. org_id omitted -> now stamps ctx.org_id (NOT NULL). THE BEHAVIOUR CHANGE:
     pin it explicitly, since today it is always NULL.
  2. org_id provided, a SECOND (non-oldest) org the caller belongs to -> the
     bundle lands in the REQUESTED org, not the oldest membership.
  3. org_id for a non-member org -> 403, and no Bundle row is created.
  4. malformed org_id -> 422.
  5. THE KEY TEST: a bundle created in org B is NOT readable by a caller
     scoped to org A — via authz.can_access_bundle AND the live
     GET /api/cookbooks list route's org filter, not a reimplemented check.
     This is the test that proves the read filter is no longer dead code.

SECURITY DISTINCTION (full argument in the PR body): this is SELECTION, not
ASSERTION (T0-B). The caller only picks among orgs the server has ALREADY
independently verified via OrgMembership — the same authority
_resolve_org_membership already uses. It grants no new access.
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
        display_name="bundleorg-user",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user, *, label="bundleorg-key"):
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


def _mk_org(db, *, name="bundleorg-org"):
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


# ── 1. org_id omitted -> now stamps ctx.org_id (the behaviour change) ─────


def test_omit_org_id_now_stamps_ctx_org_id(middleware_client, db_session):
    """No org_id in the body -> the bundle inherits ctx.org_id (the caller's
    OLDEST OrgMembership). THIS IS THE FIX: before this change the
    constructor never wrote org_id at all, so this was always NULL. Pin the
    new, correct behaviour explicitly."""
    import datetime

    user = _mk_user(db_session)
    org_old = _mk_org(db_session, name="oldest-org")
    org_new = _mk_org(db_session, name="newer-org")
    _mk_org_membership(
        db_session, org_old, user, created_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    )
    _mk_org_membership(
        db_session, org_new, user, created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    )
    key = _mk_key(db_session, user)
    db_session.commit()

    r = middleware_client.post(
        "/api/cookbooks", headers={"x-api-key": key}, json={"name": "no-org-id-bundle"}
    )
    assert r.status_code == 201, r.text
    bundle_id = r.json()["id"]

    from app.models import Bundle

    bundle = db_session.query(Bundle).filter(Bundle.id == uuid.UUID(bundle_id)).first()
    assert bundle is not None
    assert bundle.org_id == org_old.id, (
        f"omitting org_id must now stamp ctx.org_id (oldest membership), "
        f"got org_id={bundle.org_id!r}, expected oldest={org_old.id} "
        f"(NOT NULL — that was the defect)"
    )


# ── 2. org_id provided (2nd, non-oldest org) -> lands in requested org ────


def test_org_id_selects_second_non_oldest_org(middleware_client, db_session):
    """A user in TWO orgs can create a bundle in the SECOND (non-oldest) org
    by passing org_id — Bundle.org_id must equal the REQUESTED org, not the
    oldest one _resolve_org_membership would otherwise pick."""
    import datetime

    user = _mk_user(db_session)
    org_old = _mk_org(db_session, name="wisechef-ai-b")
    org_new = _mk_org(db_session, name="astrovita-b")
    _mk_org_membership(
        db_session, org_old, user, created_at=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    )
    _mk_org_membership(
        db_session, org_new, user, created_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    )
    key = _mk_key(db_session, user)
    db_session.commit()

    r = middleware_client.post(
        "/api/cookbooks",
        headers={"x-api-key": key},
        json={"name": "astrovita-bundle", "org_id": str(org_new.id)},
    )
    assert r.status_code == 201, r.text
    bundle_id = r.json()["id"]

    from app.models import Bundle

    bundle = db_session.query(Bundle).filter(Bundle.id == uuid.UUID(bundle_id)).first()
    assert bundle is not None
    assert bundle.org_id == org_new.id, (
        f"org_id selector must land the bundle in the REQUESTED org, "
        f"not the oldest membership: got {bundle.org_id}, expected {org_new.id}"
    )
    assert bundle.org_id != org_old.id


# ── 3. org_id for a NON-member org -> 403, no Bundle row created ──────────


def test_org_id_for_non_member_org_is_forbidden_and_creates_nothing(middleware_client, db_session):
    """Passing an org the caller is NOT a member of -> 403, and no Bundle row
    is created as a side effect (assert the count is unchanged)."""
    from app.models import Bundle

    user = _mk_user(db_session)
    own_org = _mk_org(db_session, name="own-org-b")
    _mk_org_membership(db_session, own_org, user)
    other_org = _mk_org(db_session, name="not-my-org-b")
    # deliberately NO membership for `user` in other_org
    key = _mk_key(db_session, user)
    db_session.commit()

    before_count = db_session.query(Bundle).count()

    r = middleware_client.post(
        "/api/cookbooks",
        headers={"x-api-key": key},
        json={"name": "should-not-exist", "org_id": str(other_org.id)},
    )
    assert r.status_code == 403, r.text

    after_count = db_session.query(Bundle).count()
    assert after_count == before_count, (
        f"forbidden org_id must create NO Bundle row: before={before_count}, after={after_count}"
    )


# ── 4. malformed org_id -> 422 ─────────────────────────────────────────────


def test_malformed_org_id_returns_422(middleware_client, db_session):
    user = _mk_user(db_session)
    org = _mk_org(db_session, name="malformed-test-org-b")
    _mk_org_membership(db_session, org, user)
    key = _mk_key(db_session, user)
    db_session.commit()

    r = middleware_client.post(
        "/api/cookbooks",
        headers={"x-api-key": key},
        json={"name": "bad-org-id-bundle", "org_id": "not-a-uuid"},
    )
    assert r.status_code == 422, r.text


# ── 5. THE KEY TEST — cross-org isolation on READ, via can_access_bundle ──


def test_bundle_in_org_b_not_readable_by_caller_scoped_to_org_a(middleware_client, db_session):
    """A bundle created (via org_id selector) in org B must NOT be readable
    by a DIFFERENT caller scoped to org A — reusing authz.can_access_bundle
    (the existing predicate the read path already enforces, previously dead
    code because org_id was always NULL) rather than reimplementing the
    check. This is the test that proves the read filter is live."""
    from app.auth_ctx import AuthContext
    from app import authz
    from app.models import Bundle

    fde_user = _mk_user(db_session)  # belongs to both orgs
    org_a = _mk_org(db_session, name="org-a-isolation-b")
    org_b = _mk_org(db_session, name="org-b-isolation-b")
    _mk_org_membership(db_session, org_a, fde_user)
    _mk_org_membership(db_session, org_b, fde_user)
    fde_key = _mk_key(db_session, fde_user)
    db_session.commit()

    # FDE creates a bundle explicitly in org B.
    r = middleware_client.post(
        "/api/cookbooks",
        headers={"x-api-key": fde_key},
        json={"name": "org-b-only-bundle", "org_id": str(org_b.id)},
    )
    assert r.status_code == 201, r.text
    bundle_b = db_session.query(Bundle).filter(Bundle.id == uuid.UUID(r.json()["id"])).first()
    assert bundle_b is not None
    assert bundle_b.org_id == org_b.id

    # A DIFFERENT caller, scoped only to org A, must not be able to read it.
    other_user = _mk_user(db_session)
    _mk_org_membership(db_session, org_a, other_user)
    db_session.commit()

    other_ctx = AuthContext(scope="user", user_id=other_user.id, org_id=org_a.id)
    assert authz.can_access_bundle(other_ctx, bundle_b) is False, (
        "a caller scoped to org A must not be authorized to read a bundle "
        "created in org B via the org_id selector"
    )

    # Sanity: the FDE, scoped to org B, CAN access it.
    fde_ctx_b = AuthContext(scope="user", user_id=fde_user.id, org_id=org_b.id)
    assert authz.can_access_bundle(fde_ctx_b, bundle_b) is True

    # End-to-end via the live route surface: org-A-scoped caller's
    # GET /api/cookbooks list does not include the org-B bundle. This is
    # the line that was dead code before this fix — Bundle.org_id was
    # always NULL, so `Bundle.org_id == ctx.org_id` could never match
    # anything, and this assertion would have passed VACUOUSLY (nothing
    # in the list, ever). It is meaningful now because org_b's bundle is
    # actually visible to org_b members (asserted below) and NOT to org_a.
    other_key = _mk_key(db_session, other_user)
    db_session.commit()
    list_resp = middleware_client.get("/api/cookbooks", headers={"x-api-key": other_key})
    assert list_resp.status_code == 200
    seen_ids = {c["id"] for c in list_resp.json()["cookbooks"]}
    assert str(bundle_b.id) not in seen_ids

    # And confirm the read filter DOES admit an org_b member (proves the
    # filter is live, not just permanently empty).
    org_b_member = _mk_user(db_session)
    _mk_org_membership(db_session, org_b, org_b_member)
    org_b_member_key = _mk_key(db_session, org_b_member)
    db_session.commit()
    list_resp_b = middleware_client.get("/api/cookbooks", headers={"x-api-key": org_b_member_key})
    assert list_resp_b.status_code == 200
    seen_ids_b = {c["id"] for c in list_resp_b.json()["cookbooks"]}
    assert str(bundle_b.id) in seen_ids_b, (
        "an org_b member must see org_b's bundle via the org-scoped read "
        "filter — this is the assertion that proves the filter is no "
        "longer dead code"
    )
