"""mesh_0408 W1 (P0) — cross-org bundle isolation for fleet-member API keys.

THE DEFECT (reproduced against production 2026-08-07, both directions):

    WiseChef member key  -> WiseChef bundle  (CONTROL) : 200
    WiseChef member key  -> ASTROVITA bundle           : 200  <- LEAK
    Astrovita member key -> WISECHEF bundle            : 200  <- LEAK

Three stacked layers produced it:

  1. IDENTITY   ``fleet_member_routes.enroll_member`` mints a member key with
     ``user_id = fleet.owner_user_id`` — the identity of the ACCOUNT running
     the fleet, not a per-client identity. Every member key of an account
     therefore collapses to one ``user_id``.
  2. ORG        ``middleware.api_key._resolve_org_membership`` picked the
     OLDEST ``OrgMembership`` row, so every key of that account resolved to
     whichever org it created first, regardless of which fleet the key serves.
  3. PREDICATE  Nine authorization sites granted on a bare
     ``bundle.bundle_owner == ctx.user_id``, which short-circuits BEFORE any
     org constraint.

Layers 2 and 3 are BOTH load-bearing. Fixing only the predicate closes exactly
one direction: a key whose true org is the oldest one still resolves to the
right org by accident and keeps reading that tenant's bundles. Each direction
below is asserted separately for that reason — see
``test_layer2_is_load_bearing_oldest_membership_does_not_win``.

TRAP V2 COMPLIANCE. A cross-tenant test whose two principals share every axis
the predicate short-circuits on proves nothing. These two principals SHARE a
``user_id`` on purpose — that is the entire defect — so this module asserts the
precondition explicitly before any probe runs:

    key_a.user_id == key_b.user_id     # same identity...
    ctx_a.org_id  != ctx_b.org_id      # ...different resolved tenant

If the second assertion fails the suite is VOID, not passing: every probe would
be denied for the wrong reason. Both CONTROL probes (same-tenant access still
works) carry the same "void, not green" contract.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

# ── Fixture builders ──────────────────────────────────────────────────────


def _mk_user(db, *, tier="pro"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="one-account-two-clients",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user, *, label="account-key"):
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


def _mk_org(db, *, name):
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

    m = OrgMembership(id=uuid.uuid4(), org_id=org.id, user_id=user.id, role=role)
    db.add(m)
    db.flush()
    return m


def _mk_fleet(db, owner, *, org_id, name):
    from app.models import Fleet

    fleet = Fleet(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        name=name,
        fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
        org_id=org_id,
    )
    db.add(fleet)
    db.flush()
    return fleet


def _mk_bundle(db, owner, *, org_id, name, slug=None, visibility="private"):
    from app.models import Bundle

    cb = Bundle(
        id=uuid.uuid4(),
        name=name,
        bundle_owner=owner.id,
        org_id=org_id,
        slug=slug,
        visibility=visibility,
    )
    db.add(cb)
    db.flush()
    return cb


def _mk_private_skill_in_bundle(db, bundle, *, slug):
    """A PRIVATE skill declared in ``bundle`` — the authz.can_read_skill surface."""
    from app.models import BundleSkill, Skill

    skill = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        description="private client skill",
        category="general",
        is_public=False,
    )
    db.add(skill)
    db.flush()
    db.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added"))
    db.flush()
    return skill


@pytest.fixture
def tenant_app(db_session, monkeypatch):
    """The real middleware stack + a ``/_probe/whoami`` auth_ctx read-back.

    The probe route is how this module asserts the trap-V2 precondition on the
    REAL resolution path: it reports whatever ``APIKeyMiddleware`` actually
    stamped, so a regression in layer 2 shows up as a changed ``org_id`` rather
    than being papered over by a hand-built context.
    """
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)

    @app.get("/_probe/whoami")
    def _whoami(request: Request):
        ctx = getattr(request.state, "auth_ctx", None)
        return {
            "scope": getattr(ctx, "scope", None),
            "user_id": str(getattr(ctx, "user_id", None)),
            "org_id": str(getattr(ctx, "org_id", None)),
            "is_org_owner": getattr(ctx, "is_org_owner", None),
        }

    return TestClient(app)


class Tenants:
    """One account, two client orgs, two fleets, two member keys, two bundles."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def tenants(tenant_app, db_session):
    """Build the production shape: ONE account running TWO isolated client orgs.

    org_a is created FIRST so its OrgMembership is the OLDEST — that is what
    ``_resolve_org_membership`` used to return for BOTH member keys, and it is
    what makes the two leak directions asymmetric.

    Member keys are minted through the REAL enrollment route
    (``POST /api/fleets/{id}/members``), not hand-inserted, so the identity
    collapse under test is the one production actually produces.
    """
    user = _mk_user(db_session)
    account_key = _mk_key(db_session, user)

    org_a = _mk_org(db_session, name="wisechef-like")
    _mk_org_membership(db_session, org_a, user)  # OLDEST membership
    org_b = _mk_org(db_session, name="astrovita-like")
    _mk_org_membership(db_session, org_b, user)

    fleet_a = _mk_fleet(db_session, user, org_id=org_a.id, name="fleet-client-a")
    fleet_b = _mk_fleet(db_session, user, org_id=org_b.id, name="fleet-client-b")

    bundle_a = _mk_bundle(db_session, user, org_id=org_a.id, name="Client A Private", slug="client-a-priv")
    bundle_b = _mk_bundle(db_session, user, org_id=org_b.id, name="Client B Private", slug="client-b-priv")

    skill_a = _mk_private_skill_in_bundle(db_session, bundle_a, slug="client-a-secret-skill")
    skill_b = _mk_private_skill_in_bundle(db_session, bundle_b, slug="client-b-secret-skill")
    db_session.commit()

    def _enroll(fleet, host):
        r = tenant_app.post(
            f"/api/fleets/{fleet.id}/members",
            headers={"x-api-key": account_key},
            json={"host": host, "profile": "default", "skills_dir": "~/.hermes/loopskill"},
        )
        assert r.status_code == 201, f"member enrollment failed — fixture void: {r.text}"
        return r.json()["api_key"]

    key_a = _enroll(fleet_a, "agent-at-client-a")
    key_b = _enroll(fleet_b, "agent-at-client-b")
    db_session.commit()

    return Tenants(
        user=user,
        account_key=account_key,
        org_a=org_a,
        org_b=org_b,
        fleet_a=fleet_a,
        fleet_b=fleet_b,
        bundle_a=bundle_a,
        bundle_b=bundle_b,
        skill_a=skill_a,
        skill_b=skill_b,
        key_a=key_a,
        key_b=key_b,
    )


def _whoami(client, key):
    r = client.get("/_probe/whoami", headers={"x-api-key": key})
    assert r.status_code == 200, r.text
    return r.json()


# ── 0. TRAP V2 — the precondition. Everything below is void without it. ────


def test_v2_precondition_same_identity_different_tenant(tenant_app, tenants):
    """The two principals MUST share a user_id and MUST NOT share an org_id.

    Shared identity is the defect being tested: an account's member keys are
    all minted with ``user_id = fleet.owner_user_id``. If the org_ids ever
    matched too, every cross-tenant probe in this module would be denied for
    the wrong reason and the suite would be VOID, not passing.
    """
    a = _whoami(tenant_app, tenants.key_a)
    b = _whoami(tenant_app, tenants.key_b)

    print(f"principal A: user_id={a['user_id']} org_id={a['org_id']}")
    print(f"principal B: user_id={b['user_id']} org_id={b['org_id']}")

    assert a["scope"] == "user" and b["scope"] == "user"
    # ...the trap: SAME identity...
    assert a["user_id"] == b["user_id"] == str(tenants.user.id), (
        "principals no longer share a user_id — the fixture stopped reproducing "
        "the defect, so every probe below is vacuous"
    )
    # ...but DIFFERENT resolved tenants. If this fails, the test is VOID.
    assert a["org_id"] != b["org_id"], (
        "both member keys resolved to the SAME org — layer 2 is broken and this module proves nothing"
    )
    assert a["org_id"] == str(tenants.org_a.id)
    assert b["org_id"] == str(tenants.org_b.id), (
        "key_b resolved to the OLDEST membership instead of ITS fleet's org — "
        "this is the exact layer-2 defect"
    )
    # A deployed agent is never the org payer (can_manage_fleet keys off this).
    assert a["is_org_owner"] is False and b["is_org_owner"] is False


def test_layer2_fails_closed_when_fleet_has_no_org(tenant_app, db_session):
    """A member key on an org-less fleet gets PERSONAL scope, not the oldest org.

    Falling back to ``_resolve_org_membership`` here would silently re-open the
    leak for exactly the fleets that carry no tenant label.
    """
    user = _mk_user(db_session)
    account_key = _mk_key(db_session, user)
    org = _mk_org(db_session, name="some-org")
    _mk_org_membership(db_session, org, user)
    personal_fleet = _mk_fleet(db_session, user, org_id=None, name="personal-fleet")
    db_session.commit()

    r = tenant_app.post(
        f"/api/fleets/{personal_fleet.id}/members",
        headers={"x-api-key": account_key},
        json={"host": "h", "profile": "default", "skills_dir": "~/x"},
    )
    assert r.status_code == 201, r.text
    member_key = r.json()["api_key"]
    db_session.commit()

    who = _whoami(tenant_app, member_key)
    assert who["org_id"] == "None", (
        "a member key on an org-less fleet inherited an org from membership — "
        "that is the layer-2 bug, fail-closed was required"
    )

    # The account's own key is unaffected — humans still resolve by membership.
    assert _whoami(tenant_app, account_key)["org_id"] == str(org.id)


# ── 1. authz.py:66 — can_read_skill's bundle-ownership clause ─────────────


def test_site_authz_can_read_skill_bundle_clause(tenant_app, tenants, db_session):
    """A private skill in client A's bundle is unreadable from client B's key.

    Asserted at the DESTINATION, not the status code (trap E2): the mirror into
    the Liked bundle is the read that would widen access.

    codex review of PR #202, finding 5 — this test USED TO BE TOOTHLESS on half
    its subject. It asserted only that no ``BundleSkill`` mirror appeared, while
    the route went on to COMMIT a ``SkillLike`` row against client A's private
    skill and answer 200 with its global ``like_count``. A cross-tenant caller
    that cannot read a row must not be able to write one referencing it, and
    must not be able to tell it apart from a slug that was never minted, so all
    three are asserted now: the mirror, the row, and the status code.
    """
    from app.models import Bundle, BundleSkill, SkillLike

    def _in_liked_bundle(skill):
        return (
            db_session.query(BundleSkill)
            .join(Bundle, Bundle.id == BundleSkill.bundle_id)
            .filter(Bundle.is_liked.is_(True), BundleSkill.skill_id == skill.id)
            .first()
            is not None
        )

    def _like_rows(skill):
        return db_session.query(SkillLike).filter(SkillLike.skill_id == skill.id).count()

    # CROSS-TENANT first, so a later control write cannot mask it.
    r = tenant_app.post(f"/api/skills/{tenants.skill_a.slug}/like", headers={"x-api-key": tenants.key_b})
    assert r.status_code == 404, (
        f"cross-tenant like leaked the existence of a private slug: {r.status_code} {r.text}"
    )
    assert r.json()["detail"] == f"Track '{tenants.skill_a.slug}' not found", (
        "the cross-tenant denial must be byte-identical to the never-existed "
        f"answer _resolve_track_identity gives: {r.text}"
    )
    assert not _in_liked_bundle(tenants.skill_a), (
        "client B's key read a PRIVATE skill out of client A's bundle via the "
        "can_read_skill bundle-ownership clause"
    )
    assert _like_rows(tenants.skill_a) == 0, (
        "client B's key COMMITTED a SkillLike row against client A's private "
        "skill — a cross-tenant caller must not mutate"
    )

    # The same must hold for the favourite verb, which writes its own row.
    r = tenant_app.post(f"/api/skills/{tenants.skill_a.slug}/favourite", headers={"x-api-key": tenants.key_b})
    assert r.status_code == 404, f"cross-tenant favourite: {r.status_code} {r.text}"
    from app.models import SkillFavourite

    assert (
        db_session.query(SkillFavourite).filter(SkillFavourite.skill_id == tenants.skill_a.id).count() == 0
    ), "client B's key COMMITTED a SkillFavourite row against client A's private skill"

    # CONTROL — same tenant still works. If this fails the suite is VOID.
    r = tenant_app.post(f"/api/skills/{tenants.skill_a.slug}/like", headers={"x-api-key": tenants.key_a})
    assert r.status_code == 200, r.text
    assert _in_liked_bundle(tenants.skill_a), (
        "CONTROL FAILED: the owning tenant can no longer read its own private "
        "skill — this suite is void, not green"
    )
    assert _like_rows(tenants.skill_a) == 1, (
        "CONTROL FAILED: the owning tenant's like no longer writes its row — void, not green"
    )


# ── 2. reconcile_routes.py:93 — the proven-leaking site ───────────────────


@pytest.mark.parametrize(
    "key_attr,bundle_attr,expect",
    [
        ("key_a", "bundle_a", 200),  # CONTROL
        ("key_b", "bundle_b", 200),  # CONTROL
        ("key_a", "bundle_b", 404),  # LEAK direction 1
        ("key_b", "bundle_a", 404),  # LEAK direction 2
    ],
)
def test_site_reconcile(tenant_app, tenants, key_attr, bundle_attr, expect):
    """The full reconcile diff — every skill, version and connector — is tenant-scoped."""
    r = tenant_app.post(
        f"/api/cookbooks/{getattr(tenants, bundle_attr).id}/reconcile",
        headers={"x-api-key": getattr(tenants, key_attr)},
        json={"local": [], "dry_run": True},
    )
    assert r.status_code == expect, f"{key_attr} -> {bundle_attr}: {r.status_code} {r.text}"
    if expect == 404:
        # Deliverable 3: 404, never 403 — a 403 confirms the artifact EXISTS.
        assert r.json()["error"] == "cookbook_not_found"


# ── 3. bundle_routes.py:356 — _resolve_owned_cookbook (~20 routes) ────────


@pytest.mark.parametrize(
    "key_attr,bundle_attr,expect",
    [
        ("key_a", "bundle_a", 200),
        ("key_b", "bundle_b", 200),
        ("key_a", "bundle_b", 404),
        ("key_b", "bundle_a", 404),
    ],
)
def test_site_bundle_detail(tenant_app, tenants, key_attr, bundle_attr, expect):
    """GET /api/cookbooks/{id} — proven live to leak name, slug and skill list."""
    r = tenant_app.get(
        f"/api/cookbooks/{getattr(tenants, bundle_attr).id}",
        headers={"x-api-key": getattr(tenants, key_attr)},
    )
    assert r.status_code == expect, f"{key_attr} -> {bundle_attr}: {r.status_code} {r.text}"


def test_bundle_detail_leaks_no_field_cross_tenant(tenant_app, tenants):
    """The 404 body must carry none of the private bundle's fields."""
    r = tenant_app.get(f"/api/cookbooks/{tenants.bundle_b.id}", headers={"x-api-key": tenants.key_a})
    assert r.status_code == 404
    body = r.text
    assert tenants.bundle_b.name not in body
    assert tenants.bundle_b.slug not in body
    assert tenants.skill_b.slug not in body


# ── 4. bundle_routes.py:1003 — the bundle LIST ────────────────────────────


def test_site_bundle_list_is_tenant_scoped(tenant_app, tenants):
    """Listing from one tenant must not enumerate the other tenant's bundles."""
    for key_attr, mine, theirs in (
        ("key_a", tenants.bundle_a, tenants.bundle_b),
        ("key_b", tenants.bundle_b, tenants.bundle_a),
    ):
        r = tenant_app.get("/api/cookbooks", headers={"x-api-key": getattr(tenants, key_attr)})
        assert r.status_code == 200, r.text
        ids = {c["id"] for c in r.json()["cookbooks"]}
        assert str(mine.id) in ids, (
            f"CONTROL FAILED for {key_attr}: own tenant's bundle missing from the "
            "list — this suite is void, not green"
        )
        assert str(theirs.id) not in ids, f"{key_attr} enumerated the OTHER tenant's bundle {theirs.name}"


# ── 5. artifact_like_routes.py:292 — the like/follow existence oracle ─────


def test_site_bundle_like_no_cross_tenant_existence_oracle(tenant_app, tenants):
    """400 vs 404 told a foreign tenant that a private slug exists. Now 404 both ways."""
    r = tenant_app.post(f"/api/bundles/{tenants.bundle_b.slug}/like", headers={"x-api-key": tenants.key_a})
    assert r.status_code == 404, f"cross-tenant like leaked existence: {r.status_code} {r.text}"

    r = tenant_app.post(f"/api/bundles/{tenants.bundle_a.slug}/like", headers={"x-api-key": tenants.key_b})
    assert r.status_code == 404, f"cross-tenant like leaked existence: {r.status_code} {r.text}"

    # CONTROL — inside its own tenant the self-follow guard still speaks plainly.
    r = tenant_app.post(f"/api/bundles/{tenants.bundle_a.slug}/like", headers={"x-api-key": tenants.key_a})
    assert r.status_code == 400 and r.json()["detail"] == "cannot_like_own_bundle", (
        f"CONTROL FAILED: same-tenant self-like no longer reports plainly — {r.text}"
    )


# ── 6. connector_routes.py:96 — _require_bundle_owner ─────────────────────


@pytest.mark.parametrize(
    "key_attr,bundle_attr,expect_denied",
    [
        ("key_a", "bundle_a", False),
        ("key_b", "bundle_b", False),
        ("key_a", "bundle_b", True),
        ("key_b", "bundle_a", True),
    ],
)
def test_site_connector_declare(tenant_app, tenants, db_session, key_attr, bundle_attr, expect_denied):
    """Declaring a connector into another tenant's bundle is a cross-tenant WRITE."""
    from app.models import Connector

    conn = db_session.query(Connector).filter(Connector.slug == "probe-connector").first()
    if conn is None:
        conn = Connector(
            id=uuid.uuid4(),
            slug="probe-connector",
            title="probe",
            connector_type="stdio",
            is_public=True,
            is_archived=False,
        )
        db_session.add(conn)
        db_session.commit()

    r = tenant_app.post(
        f"/api/bundles/{getattr(tenants, bundle_attr).id}/connectors",
        headers={"x-api-key": getattr(tenants, key_attr)},
        json={"slug": "probe-connector"},
    )
    if expect_denied:
        assert r.status_code == 404, f"{key_attr} -> {bundle_attr}: {r.status_code} {r.text}"
        assert r.json()["detail"] == "bundle_not_found"
    else:
        assert r.status_code == 201, (
            f"CONTROL FAILED ({key_attr} -> {bundle_attr}): {r.status_code} {r.text} — "
            "this suite is void, not green"
        )


# ── 7. promotion_routes.py:89 — reconcile-report / promotion gate ─────────


@pytest.mark.parametrize(
    "key_attr,bundle_attr,skill_attr,expect",
    [
        ("key_a", "bundle_a", "skill_a", 200),
        ("key_b", "bundle_b", "skill_b", 200),
        ("key_a", "bundle_b", "skill_b", 404),
        ("key_b", "bundle_a", "skill_a", 404),
    ],
)
def test_site_promotion_report(tenant_app, tenants, key_attr, bundle_attr, skill_attr, expect):
    """A foreign tenant must not be able to drive another client's promotion gate."""
    r = tenant_app.post(
        f"/api/cookbooks/{getattr(tenants, bundle_attr).id}/reconcile-report",
        headers={"x-api-key": getattr(tenants, key_attr)},
        json={"slug": getattr(tenants, skill_attr).slug, "semver": "1.0.0", "outcome": "success"},
    )
    assert r.status_code == expect, f"{key_attr} -> {bundle_attr}: {r.status_code} {r.text}"
    if expect == 404:
        assert r.json()["error"] == "cookbook_not_found"


# ── 8. recipify_routes.py:58/64 — authoring target resolution ─────────────


def test_site_recipify_explicit_target_is_tenant_scoped(tenant_app, tenants):
    """Authoring a draft INTO another tenant's bundle is a cross-tenant write."""
    body = {
        "slug": "cross-tenant-probe",
        "content": (
            "---\n"
            "name: cross-tenant-probe\n"
            "description: A probe skill authored from one tenant at another tenant.\n"
            "---\n"
            "Probe body.\n"
        ),
        "target_cookbook_id": str(tenants.bundle_b.id),
    }
    r = tenant_app.post("/api/recipify", headers={"x-api-key": tenants.key_a}, json=body)
    assert r.status_code == 404, f"cross-tenant recipify target accepted: {r.status_code} {r.text}"
    assert r.json()["detail"] == "cookbook_not_found"


def test_site_recipify_implicit_target_never_crosses_tenant(tenant_app, tenants, db_session):
    """The implicit "my oldest bundle" fallback must stay inside the caller's tenant.

    bundle_a is the account's OLDEST bundle. A draft authored from client B's
    key must never land in it.
    """
    from app.recipify_routes import _resolve_or_create_cookbook
    from app.bundle_routes import CookbookCtx

    ctx_b = CookbookCtx(user_id=tenants.user.id, is_master=False, tier="pro", org_id=tenants.org_b.id)
    resolved = _resolve_or_create_cookbook(db_session, ctx_b, None)
    assert resolved.id != tenants.bundle_a.id, (
        "the implicit authoring target resolved to the OTHER tenant's oldest bundle"
    )
    assert resolved.org_id in (None, tenants.org_b.id)


# ── 9. engagement_routes.py:323 — GET /api/me/library owned bundles ───────


def test_site_library_owned_bundles_are_tenant_scoped(db_session, tenants):
    """The library aggregates only the caller's CURRENT tenant.

    ``/api/me/library`` sits behind ``JWT_AUTH_PREFIXES``, so APIKeyMiddleware
    hands it straight through and the route reads whatever ``auth_ctx`` the
    JWT-cookie path stamped. This test therefore injects the context directly —
    the same idiom test_spotify1507_pha_engagement.py uses — because the
    x-api-key surface cannot reach this route at all.
    """
    from fastapi import FastAPI
    from app.auth_ctx import AuthContext
    from app.database import get_db
    from app.engagement_routes import router as engagement_router

    def _client_for(org_id):
        app = FastAPI()
        app.include_router(engagement_router)
        app.dependency_overrides[get_db] = lambda: db_session

        @app.middleware("http")
        async def _inject(request, call_next):
            request.state.auth_ctx = AuthContext(
                scope="user", user_id=tenants.user.id, tier="pro", org_id=org_id
            )
            return await call_next(request)

        return TestClient(app)

    for org, mine, theirs in (
        (tenants.org_a, tenants.bundle_a, tenants.bundle_b),
        (tenants.org_b, tenants.bundle_b, tenants.bundle_a),
    ):
        r = _client_for(org.id).get("/api/me/library")
        assert r.status_code == 200, r.text
        ids = {b["id"] for b in r.json()["owned_bundles"]}
        assert str(mine.id) in ids, f"CONTROL FAILED for org {org.name}: own bundle missing — void, not green"
        assert str(theirs.id) not in ids, f"library for org {org.name} listed the OTHER tenant's bundle"


# ── Layer-2 load-bearing proof (asymmetry) ────────────────────────────────


def test_layer2_is_load_bearing_oldest_membership_does_not_win(tenant_app, tenants):
    """Pin the direction that a predicate-only fix would MISS.

    With layer 3 alone, key_b still resolves to org_a (the oldest membership),
    so ``key_b -> bundle_a`` compares org_a == org_a and is ALLOWED — half the
    leak survives. This test fails if layer 2 is ever reverted, even with the
    tenant predicate fully in place.
    """
    assert _whoami(tenant_app, tenants.key_b)["org_id"] == str(tenants.org_b.id), (
        "key_b resolved to the oldest membership — layer 2 has regressed"
    )
    r = tenant_app.get(f"/api/cookbooks/{tenants.bundle_a.id}", headers={"x-api-key": tenants.key_b})
    assert r.status_code == 404, (
        "key_b reached the OLDEST org's bundle — the layer-2 fleet-org "
        f"resolution is not doing its job: {r.status_code} {r.text}"
    )


# ── The tenant predicate itself (the shared surface all 9 sites use) ──────


class TestTenantPredicate:
    """Exhaustive truth table for authz.owner_match_within_tenant + its SQL twin.

    Pinned as a unit because nine call sites depend on these exact semantics,
    and because the SQL clause and the row predicate MUST agree — a mismatch
    means a detail route and its list route disagree about who can see what.
    """

    @staticmethod
    def _ctx(user_id, org_id):
        from app.auth_ctx import AuthContext

        return AuthContext(scope="user", user_id=user_id, org_id=org_id)

    @staticmethod
    def _obj(owner, org_id):
        class _B:
            bundle_owner = owner
            org_id = None

        b = _B()
        b.org_id = org_id
        return b

    def test_truth_table(self):
        from app import authz

        uid, other = uuid.uuid4(), uuid.uuid4()
        org_a, org_b = uuid.uuid4(), uuid.uuid4()

        # personal caller, personal artifact → owner-match applies (back-compat)
        assert authz.owner_match_within_tenant(self._ctx(uid, None), self._obj(uid, None))
        # same tenant → allowed
        assert authz.owner_match_within_tenant(self._ctx(uid, org_a), self._obj(uid, org_a))
        # different tenant → DENY (this is the P0)
        assert not authz.owner_match_within_tenant(self._ctx(uid, org_a), self._obj(uid, org_b))
        # personal caller, org artifact → DENY
        assert not authz.owner_match_within_tenant(self._ctx(uid, None), self._obj(uid, org_a))
        # org caller, personal artifact → allowed (predates tenancy, back-compat)
        assert authz.owner_match_within_tenant(self._ctx(uid, org_a), self._obj(uid, None))
        # not the owner → DENY regardless of tenant
        assert not authz.owner_match_within_tenant(self._ctx(other, org_a), self._obj(uid, org_a))
        # no identity → DENY (never degrade to `bundle_owner IS NULL`)
        assert not authz.owner_match_within_tenant(self._ctx(None, None), self._obj(None, None))

    def test_master_keeps_its_bypass(self):
        from app import authz
        from app.auth_ctx import AuthContext

        obj = self._obj(uuid.uuid4(), uuid.uuid4())
        assert authz.owner_match_within_tenant(AuthContext(scope="master"), obj)
        assert not authz.crosses_tenant(AuthContext(scope="master"), obj)

    def test_sql_clause_agrees_with_row_predicate(self, db_session):
        """The clause and the predicate must select the SAME rows. Drift is the bug."""
        from app import authz
        from app.models import Bundle

        user = _mk_user(db_session)
        org_a = _mk_org(db_session, name="clause-a")
        org_b = _mk_org(db_session, name="clause-b")
        rows = [
            _mk_bundle(db_session, user, org_id=None, name="personal"),
            _mk_bundle(db_session, user, org_id=org_a.id, name="in-a"),
            _mk_bundle(db_session, user, org_id=org_b.id, name="in-b"),
        ]
        db_session.commit()
        ids = {r.id for r in rows}

        for ctx_org in (None, org_a.id, org_b.id):
            ctx = self._ctx(user.id, ctx_org)
            by_clause = {
                b.id
                for b in db_session.query(Bundle)
                .filter(Bundle.id.in_(ids), authz.owner_match_within_tenant_clause(ctx, Bundle))
                .all()
            }
            by_predicate = {r.id for r in rows if authz.owner_match_within_tenant(ctx, r)}
            assert by_clause == by_predicate, (
                f"SQL clause and row predicate disagree for ctx.org_id={ctx_org}"
            )
