"""autopilot_0308 M1 — the bundle cap meters PRIVATE bundles only (D-011).

The defect this suite pins: ``bundle_routes.create_cookbook`` counted **every**
bundle a user owned against their tier cap, so publishing a bundle for the
community *cost the curator a slot*. That inverts the business model — hub
D-011 (Adam, verbatim):

    "the public are inf and it's beneficial for us as this will make the
    platform growth (free curation by the community) scaling the offering by
    cross matching across federations."

Public bundles are therefore **unlimited on every tier, including Free**.

``visibility`` has three values, not two. The decision pinned here is that the
cap meters everything that is **NOT public** — i.e. ``private`` *and* ``team``.
Rationale: the cap exists to meter private-to-you capacity. ``team`` is not free
community curation, and counting only ``private`` would hand every user a
trivial bypass (flip everything to ``team`` → unlimited quasi-private bundles).

The cap is enforced in two places — REST (``app/bundle_routes.py``) and MCP
(``app/mcp/tools/bundle_stream.py``). Both must agree, which is why every
behavioural assertion below is made twice, once per surface.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from typing import Generator
from uuid import uuid4

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth_ctx import AuthContext
from app.database import get_db
from app.mcp.tools.bundle_install import CookbookInstallError
from app.mcp.tools.bundle_stream import loopskill_compose_bundle_from_links
from app.models import Base, Bundle, BundleSkill, Skill, User
from app.tier_labels import bundle_limit

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ─────────────────────────── Fixtures ───────────────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ─────────────────────────── Helpers ────────────────────────────────────


def _make_user(db: Session, *, tier: str | None, status: str | None = "active") -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="M1 Tester",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(user)
    db.flush()
    return user


def _seed_bundles(db: Session, owner, n: int, visibility: str) -> list[Bundle]:
    """Seed ``n`` bundles of the given visibility directly (fast path)."""
    out = []
    for _ in range(n):
        cb = Bundle(
            id=uuid4(),
            name=f"{visibility}-{uuid4().hex[:8]}",
            bundle_owner=owner.id if owner is not None else None,
            slug=f"{visibility}-{uuid4().hex[:12]}",
            visibility=visibility,
        )
        db.add(cb)
        out.append(cb)
    db.flush()
    return out


def _make_app(db: Session, *, api_key_user_id) -> FastAPI:
    from app.bundle_routes import router as cookbook_router

    app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    _uid = api_key_user_id

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = _uid
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(cookbook_router)
    return app


def _mk_skill(db: Session, slug: str) -> Skill:
    s = Skill(id=uuid4(), slug=slug, title=f"Skill {slug}", description="x", is_public=True)
    db.add(s)
    db.flush()
    return s


def _mk_public_source_bundle(db: Session, owner, slug: str) -> Bundle:
    """A PUBLIC bundle with one member — a valid ``bundle://`` compose source."""
    cb = Bundle(
        id=uuid4(),
        name=f"src {slug}",
        bundle_owner=owner.id if owner is not None else None,
        slug=slug,
        visibility="public",
    )
    db.add(cb)
    db.flush()
    sk = _mk_skill(db, f"sk-{uuid4().hex[:8]}")
    db.add(BundleSkill(bundle_id=cb.id, skill_id=sk.id, source="custom-added"))
    db.flush()
    return cb


def _ctx(user, tier: str | None) -> AuthContext:
    return AuthContext(scope="user", user_id=user.id, tier=tier)


ALL_TIERS = ["free", "pro", "pro_plus"]


# ══════════════════════ 1. Public bundles are never metered ══════════════


class TestPublicBundlesAreNeverMeteredRest:
    """D-011: public curation is free and unlimited on EVERY tier."""

    @pytest.mark.parametrize("tier", ALL_TIERS)
    def test_over_limit_in_public_bundles_still_allows_a_new_bundle(self, db_session, tier):
        """A curator sitting on (cap + 3) PUBLIC bundles is never blocked."""
        user = _make_user(db_session, tier=tier)
        cap = bundle_limit(tier)
        assert cap is not None, "this test only makes sense for a capped tier"
        _seed_bundles(db_session, user, cap + 3, "public")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post("/api/cookbooks", json={"name": "One More"})
        assert r.status_code == 201, (
            f"tier={tier}: {cap + 3} PUBLIC bundles must consume ZERO slots (D-011), got {r.text}"
        )

    @pytest.mark.parametrize("tier", ALL_TIERS)
    def test_publish_loop_never_terminates(self, db_session, tier):
        """create → publish → create → publish … must run past the cap forever.

        This is the exact flow a community curator performs: bundles are born
        private (inherited lock #4) and get flipped public with one click. If
        published bundles were metered, this loop would 403 at ``cap + 1``.
        """
        user = _make_user(db_session, tier=tier)
        cap = bundle_limit(tier)
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            for i in range(cap + 2):
                created = client.post("/api/cookbooks", json={"name": f"Community {i}"})
                assert created.status_code == 201, (
                    f"tier={tier}: publish-loop iteration {i} must not be capped, got {created.text}"
                )
                cb_id = created.json()["id"]
                flip = client.patch(f"/api/cookbooks/{cb_id}/visibility", json={"visibility": "public"})
                assert flip.status_code == 200, flip.text

    def test_public_bundles_do_not_consume_private_slots(self, db_session):
        """Free user: 5 public + 1 private → the 2nd private is still allowed."""
        user = _make_user(db_session, tier="free")
        _seed_bundles(db_session, user, 5, "public")
        _seed_bundles(db_session, user, 1, "private")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post("/api/cookbooks", json={"name": "Second private"})
            assert r.status_code == 201, f"free cap is 2 PRIVATE; 5 public must not count: {r.text}"
            r3 = client.post("/api/cookbooks", json={"name": "Third private"})
        assert r3.status_code == 403, "the 3rd PRIVATE bundle is still capped"


class TestPublicBundlesAreNeverMeteredMcp:
    """Same guarantee over the MCP compose verb — REST/MCP must not diverge."""

    @pytest.mark.parametrize("tier", ALL_TIERS)
    def test_over_limit_in_public_bundles_still_allows_compose(self, db_session, tier):
        user = _make_user(db_session, tier=tier)
        cap = bundle_limit(tier)
        _seed_bundles(db_session, user, cap + 3, "public")
        src = _mk_public_source_bundle(db_session, user, f"m1-src-{uuid.uuid4().hex[:8]}")
        db_session.commit()

        out = loopskill_compose_bundle_from_links(
            db_session, links=[f"bundle://{src.slug}"], ctx=_ctx(user, tier)
        )
        assert out["skill_count"] == 1, (
            f"tier={tier}: MCP compose must ignore PUBLIC bundles when metering (D-011)"
        )


# ══════════════════════ 2. Private bundles are still metered ═════════════


class TestPrivateBundlesAreMeteredRest:
    def test_free_third_private_is_403_pro_tier_limit(self, db_session):
        """The 403 contract is external: reason + max_cookbooks must survive."""
        user = _make_user(db_session, tier="free")
        _seed_bundles(db_session, user, 2, "private")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post("/api/cookbooks", json={"name": "Third"})
        assert r.status_code == 403
        detail = r.json()["detail"]
        assert detail["reason"] == "pro_tier_limit", "legacy reason code is an external contract"
        assert detail["max_cookbooks"] == 2, "legacy max_cookbooks key must stay"
        assert detail["max_private_bundles"] == 2, "additive, visibility-qualified key"
        assert detail["private_bundles_used"] == 2

    def test_team_visibility_counts_toward_the_cap(self, db_session):
        """DECIDED (M1): the cap meters NOT-public, so ``team`` counts.

        Counting only ``private`` would be a one-click cap bypass: flip every
        bundle to ``team`` and own unlimited quasi-private bundles. ``team`` is
        not free community curation, so it is not free.
        """
        user = _make_user(db_session, tier="free")
        _seed_bundles(db_session, user, 2, "team")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r = client.post("/api/cookbooks", json={"name": "Third"})
        assert r.status_code == 403, "2 'team' bundles fill a free user's cap — team is metered"
        assert r.json()["detail"]["reason"] == "pro_tier_limit"

    def test_owner_less_base_catalog_bundle_counts_against_nobody(self, db_session):
        """The ``is_base`` catalog bundle has ``bundle_owner = NULL`` BY DESIGN."""
        base = Bundle(
            id=uuid4(),
            name="WiseChef Recipes Catalog",
            bundle_owner=None,
            is_base=True,
            visibility="private",
        )
        db_session.add(base)
        user = _make_user(db_session, tier="free")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            r1 = client.post("/api/cookbooks", json={"name": "First"})
            r2 = client.post("/api/cookbooks", json={"name": "Second"})
        assert r1.status_code == 201, r1.text
        assert r2.status_code == 201, "the owner-less base bundle must not eat a user's slot"


class TestPrivateBundlesAreMeteredMcp:
    def test_compose_blocked_at_the_private_cap(self, db_session):
        user = _make_user(db_session, tier="free")
        _seed_bundles(db_session, user, 2, "private")
        src = _mk_public_source_bundle(db_session, user, f"m1-src-{uuid.uuid4().hex[:8]}")
        db_session.commit()

        with pytest.raises(CookbookInstallError) as ei:
            loopskill_compose_bundle_from_links(
                db_session, links=[f"bundle://{src.slug}"], ctx=_ctx(user, "free")
            )
        assert ei.value.code == "cookbook_limit", "MCP error code is an external contract"
        assert ei.value.status == 403

    def test_compose_meters_team_visibility(self, db_session):
        user = _make_user(db_session, tier="free")
        _seed_bundles(db_session, user, 2, "team")
        src = _mk_public_source_bundle(db_session, user, f"m1-src-{uuid.uuid4().hex[:8]}")
        db_session.commit()

        with pytest.raises(CookbookInstallError) as ei:
            loopskill_compose_bundle_from_links(
                db_session, links=[f"bundle://{src.slug}"], ctx=_ctx(user, "free")
            )
        assert ei.value.code == "cookbook_limit", "MCP must meter 'team' exactly like REST does"


# ══════════════════════ 3. The visibility flip frees a slot ══════════════


class TestVisibilityFlipFreesASlot:
    """Inherited lock #4 — one-click publish. The meter must see it instantly."""

    def test_flip_to_public_frees_a_slot_in_the_same_request_cycle(self, db_session):
        user = _make_user(db_session, tier="free")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            first = client.post("/api/cookbooks", json={"name": "A"})
            second = client.post("/api/cookbooks", json={"name": "B"})
            assert first.status_code == 201 and second.status_code == 201

            blocked = client.post("/api/cookbooks", json={"name": "C"})
            assert blocked.status_code == 403, "free user is at the cap"

            flip = client.patch(
                f"/api/cookbooks/{first.json()['id']}/visibility",
                json={"visibility": "public"},
            )
            assert flip.status_code == 200, flip.text
            assert flip.json()["visibility"] == "public"

            # No new client, no re-auth, no cache warm-up: the very next call.
            freed = client.post("/api/cookbooks", json={"name": "C again"})
        assert freed.status_code == 201, (
            f"publishing must free a slot within the same request cycle, got {freed.text}"
        )

    def test_flip_back_to_private_re_consumes_the_slot(self, db_session):
        user = _make_user(db_session, tier="free")
        db_session.commit()

        app = _make_app(db_session, api_key_user_id=user.id)
        with TestClient(app) as client:
            a = client.post("/api/cookbooks", json={"name": "A"}).json()["id"]
            client.post("/api/cookbooks", json={"name": "B"})
            client.patch(f"/api/cookbooks/{a}/visibility", json={"visibility": "public"})
            assert client.post("/api/cookbooks", json={"name": "C"}).status_code == 201

            client.patch(f"/api/cookbooks/{a}/visibility", json={"visibility": "private"})
            r = client.post("/api/cookbooks", json={"name": "D"})
        assert r.status_code == 403, "un-publishing puts the bundle back on the meter"


# ══════════════════════ 4. One counting implementation ══════════════════


class TestSingleCountingImplementation:
    """Premortem #3: a second count query is how UI and API drift apart."""

    def test_no_owner_bundle_count_query_outside_the_quota_helper(self):
        pattern = re.compile(
            r"query\(\s*Bundle\s*\)\.filter\((?:[^()]|\([^()]*\))*"
            r"Bundle\.bundle_owner(?:[^()]|\([^()]*\))*\)\.count\(\)"
        )
        offenders = []
        for path in sorted((REPO_ROOT / "app").rglob("*.py")):
            if path.name == "bundle_quota.py":
                continue
            flat = re.sub(r"\s+", " ", path.read_text())
            if pattern.search(flat):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == [], (
            "owner-bundle counting must live ONLY in app/services/bundle_quota.py — "
            f"duplicate implementations found in: {offenders}"
        )

    def test_helper_is_the_shared_import_for_every_enforcer(self):
        for rel in ("app/bundle_routes.py", "app/mcp/tools/bundle_stream.py"):
            src = (REPO_ROOT / rel).read_text()
            assert "bundle_quota" in src, f"{rel} must enforce via the shared quota helper"


class TestQuotaHelperUnit:
    def test_count_metered_bundles_ignores_public(self, db_session):
        from app.services.bundle_quota import count_metered_bundles

        user = _make_user(db_session, tier="free")
        _seed_bundles(db_session, user, 4, "public")
        _seed_bundles(db_session, user, 2, "private")
        _seed_bundles(db_session, user, 1, "team")
        db_session.commit()

        assert count_metered_bundles(db_session, user.id) == 3, "private + team, never public"

    def test_count_metered_bundles_is_owner_scoped(self, db_session):
        from app.services.bundle_quota import count_metered_bundles

        mine = _make_user(db_session, tier="free")
        theirs = _make_user(db_session, tier="free")
        _seed_bundles(db_session, theirs, 3, "private")
        db_session.commit()

        assert count_metered_bundles(db_session, mine.id) == 0

    def test_count_metered_bundles_never_counts_owner_less_rows(self, db_session):
        from app.services.bundle_quota import count_metered_bundles

        _seed_bundles(db_session, None, 2, "private")
        user = _make_user(db_session, tier="free")
        db_session.commit()

        assert count_metered_bundles(db_session, user.id) == 0
        assert count_metered_bundles(db_session, None) == 0, "no user → no quota to spend"

    def test_quota_status_reports_used_limit_and_blocked(self, db_session):
        from app.services.bundle_quota import quota_status

        user = _make_user(db_session, tier="free")
        _seed_bundles(db_session, user, 1, "private")
        _seed_bundles(db_session, user, 9, "public")
        db_session.commit()

        status = quota_status(db_session, user.id, "free")
        assert status["used"] == 1
        assert status["limit"] == 2
        assert status["blocked"] is False

        _seed_bundles(db_session, user, 1, "team")
        db_session.commit()
        assert quota_status(db_session, user.id, "free")["blocked"] is True


# ══════════════════════ 5. Display consumers agree with the enforcer ═════


class TestDisplayConsumersAgree:
    def test_conversion_gate_meters_the_same_bundles(self, db_session):
        from app.services.conversion_gates import gate_bundle_create

        user = _make_user(db_session, tier="free")
        _seed_bundles(db_session, user, 9, "public")
        _seed_bundles(db_session, user, 1, "private")
        db_session.commit()

        assert gate_bundle_create(db_session, user.id, "free").allowed is True
        _seed_bundles(db_session, user, 1, "private")
        db_session.commit()
        blocked = gate_bundle_create(db_session, user.id, "free")
        assert blocked.allowed is False
        assert blocked.http_status == 403

    def test_billing_me_reports_the_enforced_numbers(self, db_session):
        from app import auth_routes as ar
        from app.checkout_routes import _reconcile_last_attempt
        from app.checkout_routes import router as checkout_router

        user = _make_user(db_session, tier="pro", status="active")
        _seed_bundles(db_session, user, 3, "private")
        _seed_bundles(db_session, user, 7, "public")
        db_session.commit()
        _reconcile_last_attempt.pop(str(user.id), None)

        app = FastAPI()

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[ar.get_current_user_optional] = lambda: user
        app.include_router(checkout_router)

        body = TestClient(app).get("/api/billing/me").json()
        assert body["cookbook_limit"] == bundle_limit("pro"), "legacy key preserved"
        assert body["max_private_bundles"] == bundle_limit("pro")
        assert body["private_bundles_used"] == 3, "7 public bundles are not on the meter"

    def test_auth_me_reports_the_enforced_numbers(self, db_session):
        from app import auth_routes as ar
        from app.auth import create_jwt

        user = _make_user(db_session, tier="pro", status="active")
        _seed_bundles(db_session, user, 4, "private")
        _seed_bundles(db_session, user, 6, "public")
        db_session.commit()

        app = FastAPI()

        def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.include_router(ar.router)

        token = create_jwt(user)
        resp = TestClient(app).get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        features = resp.json()["features"]
        assert features["cookbook_limit"] == bundle_limit("pro"), "legacy key preserved"
        assert features["max_private_bundles"] == bundle_limit("pro")
        assert features["private_bundles_used"] == 4


# ══════════════════════ 6. D-004 — Pro 10 → 50, price untouched ══════════


class TestProLadderD004:
    def test_pro_bundle_limit_is_fifty(self):
        assert bundle_limit("pro") == 50, "D-004: Pro private-bundle cap 10 → 50"

    def test_legacy_cook_alias_shares_the_new_pro_cap(self):
        assert bundle_limit("cook") == 50

    def test_pro_price_is_unchanged(self):
        data = yaml.safe_load((REPO_ROOT / "config" / "tiers.yaml").read_text())
        assert data["tiers"]["pro"]["price_usd"] == 9.95, (
            "D-004 is explicit: the cap moves, the price does NOT"
        )
        assert data["tiers"]["pro"]["bundle_limit"] == 50

    def test_free_and_pro_plus_caps_unchanged(self):
        assert bundle_limit("free") == 2, "D-002 — free stays at 2"
        assert bundle_limit("pro_plus") == 200


# ══════════════════════ 7. The load-bearing comment is qualified ═════════


class TestCapSemanticsAreDocumented:
    """The ambiguous 'Cookbook cap' comment is what produced this defect."""

    def test_tiers_yaml_documents_the_visibility_qualifier(self):
        text = (REPO_ROOT / "config" / "tiers.yaml").read_text().lower()
        assert "public" in text and "unlimited" in text, (
            "config/tiers.yaml must state that public bundles are unlimited"
        )

    def test_terminology_documents_the_meter(self):
        text = (REPO_ROOT / "TERMINOLOGY.md").read_text().lower()
        assert "bundle_limit" in text
        assert "public" in text and "unlimited" in text

    def test_create_route_docstring_states_the_semantics(self):
        from app.bundle_routes import create_cookbook

        doc = (create_cookbook.__doc__ or "").lower()
        assert "public" in doc and "unlimited" in doc, (
            "the OpenAPI description for POST /api/cookbooks must say public is free"
        )
