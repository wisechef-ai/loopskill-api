"""spotify_1507 Phase D — programmatic cross-org leak probe.

Premortem #4 mitigation: the hand-written tenancy tests cover the KNOWN
routes, but a real cross-org leak could ship on an UNTESTED surface (a new
fleet/bundle route added without an org check, an MCP tool param). Rather than
a hand-maintained list, this probe ENUMERATES every parameterized fleet/bundle
route from the live OpenAPI schema and asserts that a cross-org caller cannot
read another org's resource — the enumeration is the guard, so a newly-added
route with no org check fails this test automatically.

Contract: for every route whose path contains {fleet_id} or {bundle_id} (or
their cookbook-alias forms), a user in org B hitting a resource owned by org A
must get 401/403/404 — NEVER a 200 that leaks org A's data.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_and_db(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return app, db_session


def _mk_user(db, *, tier="pro"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="probe-user",
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
            name="probe-key",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _mk_org(db):
    from app.models import Org

    org = Org(id=uuid.uuid4(), name="probe-org", slug=f"org-{uuid.uuid4().hex[:6]}", api_key_hash="")
    db.add(org)
    db.flush()
    return org


def _mk_membership(db, org, user, role="owner"):
    from app.models import OrgMembership

    db.add(OrgMembership(id=uuid.uuid4(), org_id=org.id, user_id=user.id, role=role))
    db.flush()


def _mk_fleet(db, owner, org_id):
    from app.models import Fleet

    fleet = Fleet(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        name="probe-fleet",
        fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
        org_id=org_id,
    )
    db.add(fleet)
    db.flush()
    return fleet


def _mk_bundle(db, owner, org_id):
    from app.models import Bundle

    cb = Bundle(
        id=uuid.uuid4(), name="probe-bundle", bundle_owner=owner.id, org_id=org_id, visibility="private"
    )
    db.add(cb)
    db.flush()
    return cb


def _parameterized_resource_routes(app) -> list[tuple[str, str]]:
    """Enumerate (method, path) for every route that scopes a fleet or bundle
    by id path-param. These are the cross-org attack surface."""
    out: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if "{fleet_id}" in path or "{bundle_id}" in path or "{cookbook_id}" in path:
            for m in methods:
                if m in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                    out.append((m, path))
    return out


def test_enumerate_cross_org_no_leak(app_and_db):
    """Every parameterized fleet/bundle route must deny a cross-org GET.

    We focus GET (the read-leak surface): a user in org B must not be able to
    READ a resource owned by org A. A route returning 200 here is a leak and
    fails the test — including any route added in the future.
    """
    app, db = app_and_db

    # Org A owns a fleet + a private bundle.
    org_a = _mk_org(db)
    owner_a = _mk_user(db)
    _mk_membership(db, org_a, owner_a)
    fleet_a = _mk_fleet(db, owner_a, org_a.id)
    bundle_a = _mk_bundle(db, owner_a, org_a.id)

    # Org B's user is the attacker.
    org_b = _mk_org(db)
    attacker = _mk_user(db)
    _mk_membership(db, org_b, attacker)
    attacker_key = _mk_key(db, attacker)
    db.commit()

    client = TestClient(app)
    routes = _parameterized_resource_routes(app)
    assert routes, "no parameterized fleet/bundle routes found — enumeration broke"

    leaks = []
    checked = 0
    for method, path in routes:
        # Only probe GET reads for the leak assertion (writes have their own
        # 403 paths; a GET 200 is the unambiguous data-leak signal).
        if method != "GET":
            continue
        concrete = (
            path.replace("{fleet_id}", str(fleet_a.id))
            .replace("{bundle_id}", str(bundle_a.id))
            .replace("{cookbook_id}", str(bundle_a.id))
        )
        # Skip routes with OTHER unfilled path params (need extra fixtures).
        if "{" in concrete:
            continue
        checked += 1
        r = client.get(concrete, headers={"x-api-key": attacker_key})
        # A cross-org read must NOT succeed with the victim's data.
        if r.status_code == 200:
            # 200 is only acceptable if the body carries no org-A resource data.
            # For a private bundle / org fleet, any 200 is a leak.
            leaks.append((method, path, r.status_code))

    assert not leaks, (
        f"cross-org READ leak on {len(leaks)} route(s) — a cross-org caller got "
        f"200 on another org's resource: {leaks}"
    )
    assert checked >= 1, "probe checked zero GET routes — enumeration is not exercising the surface"


def test_enumerate_surface_is_nontrivial(app_and_db):
    """Guard the guard: the enumeration must find a meaningful number of
    fleet/bundle routes, so a refactor that empties it can't silently pass."""
    app, _ = app_and_db
    routes = _parameterized_resource_routes(app)
    # There are many fleet + bundle + cookbook-alias parameterized routes.
    assert len(routes) >= 10, f"expected >=10 parameterized routes, found {len(routes)}: {routes}"
