"""spotify_2607 Phase B — like works on skills, bundles, personalities, loops.

Acceptance gates (from the execution plan, verified with real TestClient
output in the PR body):
- Like + unlike round-trips for ALL FOUR types
- GET /api/library returns populated skills, personalities, loops shelves
  plus followed bundles
- A test proves liking a bundle does NOT create likes for its members
- The existing /api/skills/{slug}/like still works byte-identically
- Tier/authz gating applies per artifact type
- §0b: federated likes carry a provenance (vetted|community) badge
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.artifact_like_routes import router as artifact_like_router
from app.auth_ctx import AuthContext
from app.bundle_routes import router as bundle_router
from app.database import get_db
from app.engagement_routes import router as engagement_router
from app.follow_routes import router as follow_router
from app.library_routes import router as library_router
from app.models import (
    Bundle,
    BundleCompositeLoop,
    BundlePersonality,
    BundleSkill,
    CompositeLoop,
    FollowedBundle,
    Personality,
    Skill,
    User,
)


def _app(db: Session, user_id) -> FastAPI:
    """App with engagement + artifact_like + library + bundle + follow routers."""
    app = FastAPI()

    def override_get_db():
        yield db

    @app.middleware("http")
    async def inject_auth(request: Request, call_next):
        request.state.auth_ctx = AuthContext(
            scope="user", user_id=user_id, api_key_id=None, tier="free"
        )
        return await call_next(request)

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(engagement_router)
    app.include_router(artifact_like_router)
    app.include_router(library_router)
    app.include_router(bundle_router)
    app.include_router(follow_router)
    return app


def _seed_user(db: Session, tier: str = "free") -> tuple[User, object]:
    uid = uuid4()
    u = User(
        id=uid, email=f"user-{uid.hex[:8]}@test.com", display_name="Test", subscription_tier=tier
    )
    db.add(u)
    db.commit()
    return u, uid


def _seed_skill(db: Session, slug: str = "memory") -> Skill:
    s = Skill(slug=slug, title=slug, description=f"Test {slug}", tier="free", kind="skill")
    db.add(s)
    db.commit()
    return s


def _seed_personality(db: Session, slug: str = "helpful-soul") -> Personality:
    p = Personality(
        slug=slug, title=slug, system_prompt="Be helpful.", tier="free", is_public=True
    )
    db.add(p)
    db.commit()
    return p


def _seed_loop(db: Session, slug: str = "dreaming") -> CompositeLoop:
    cl = CompositeLoop(
        slug=slug,
        title=slug,
        schedule="1h",
        skills=[],
        connectors=[],
        subagents_config={},
        verifier_slug="check",
        state_seed={},
        prompt="Do the work.",
        tier="free",
        is_public=True,
    )
    db.add(cl)
    db.commit()
    return cl


def _seed_bundle(db: Session, owner_id, slug: str = "public-bundle") -> Bundle:
    b = Bundle(
        id=uuid4(),
        name=slug,
        slug=slug,
        visibility="public",
        is_base=False,
        bundle_owner=owner_id,
    )
    db.add(b)
    db.commit()
    return b


# ── Round-trip: personality ───────────────────────────────────────────────


def test_personality_like_round_trip(db_session):
    u, uid = _seed_user(db_session)
    _seed_personality(db_session, "helpful-soul")
    client = TestClient(_app(db_session, uid))

    r = client.post("/api/personalities/helpful-soul/like")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["liked"] is True
    assert body["like_count"] == 1

    # Idempotent
    r2 = client.post("/api/personalities/helpful-soul/like")
    assert r2.status_code == 200
    assert r2.json()["like_count"] == 1

    # Unlike
    r3 = client.delete("/api/personalities/helpful-soul/like")
    assert r3.status_code == 200
    assert r3.json()["liked"] is False
    assert r3.json()["like_count"] == 0


def test_personality_like_404_when_missing(db_session):
    u, uid = _seed_user(db_session)
    client = TestClient(_app(db_session, uid))
    r = client.post("/api/personalities/nope/like")
    assert r.status_code == 404


# ── Round-trip: composite loop ────────────────────────────────────────────


def test_loop_like_round_trip(db_session):
    u, uid = _seed_user(db_session)
    _seed_loop(db_session, "dreaming")
    client = TestClient(_app(db_session, uid))

    r = client.post("/api/loops/dreaming/like")
    assert r.status_code == 200, r.text
    assert r.json()["liked"] is True
    assert r.json()["like_count"] == 1

    # Alias prefix works too
    r_alias = client.post("/api/composite-loops/dreaming/like")
    assert r_alias.status_code == 200

    r3 = client.delete("/api/loops/dreaming/like")
    assert r3.status_code == 200
    assert r3.json()["liked"] is False


# ── Round-trip: bundle (= follow) ─────────────────────────────────────────


def test_bundle_like_round_trip(db_session):
    owner, owner_id = _seed_user(db_session)
    follower, follower_id = _seed_user(db_session)
    b = _seed_bundle(db_session, owner_id, "public-bundle")
    client = TestClient(_app(db_session, follower_id))

    r = client.post("/api/bundles/public-bundle/like")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["liked"] is True
    assert body["bundle_id"] == str(b.id)
    assert body["follower_count"] == 1

    # Idempotent
    client.post("/api/bundles/public-bundle/like")
    db_session.refresh(b)
    assert b.follower_count == 1

    r3 = client.delete("/api/bundles/public-bundle/like")
    assert r3.status_code == 200
    assert r3.json()["liked"] is False
    db_session.refresh(b)
    assert b.follower_count == 0


def test_bundle_like_rejects_own_bundle(db_session):
    owner, owner_id = _seed_user(db_session)
    _seed_bundle(db_session, owner_id, "my-own")
    client = TestClient(_app(db_session, owner_id))
    r = client.post("/api/bundles/my-own/like")
    assert r.status_code == 400


def test_bundle_like_rejects_private_bundle(db_session):
    owner, owner_id = _seed_user(db_session)
    follower, follower_id = _seed_user(db_session)
    b = Bundle(
        id=uuid4(), name="priv", slug="private-bundle", visibility="private", is_base=False,
        bundle_owner=owner_id,
    )
    db_session.add(b)
    db_session.commit()
    client = TestClient(_app(db_session, follower_id))
    r = client.post("/api/bundles/private-bundle/like")
    assert r.status_code == 403


# ── Round-trip: skill (compat — existing /api/skills/{slug}/like) ─────────


def test_skill_like_compat_unchanged(db_session):
    """The shipped heart control posts to /api/skills/{slug}/like — byte-identical."""
    u, uid = _seed_user(db_session)
    _seed_skill(db_session, "memory")
    client = TestClient(_app(db_session, uid))

    r = client.post("/api/skills/memory/like")
    assert r.status_code == 200, r.text
    body = r.json()
    # Frozen response shape from engagement_routes.LikeResponse
    assert set(body) == {"liked", "like_count"}
    assert body == {"liked": True, "like_count": 1}

    r2 = client.delete("/api/skills/memory/like")
    assert r2.status_code == 200
    assert r2.json() == {"liked": False, "like_count": 0}


# ── NO CASCADE: liking a bundle must not like its members (§0 #8) ─────────


def test_liking_bundle_does_not_cascade(db_session):
    """Pin the no-cascade rule. Spotify shipped cascade-by-accident and reverted."""
    owner, owner_id = _seed_user(db_session)
    follower, follower_id = _seed_user(db_session)
    skill = _seed_skill(db_session, "member-skill")
    p = _seed_personality(db_session, "member-personality")
    cl = _seed_loop(db_session, "member-loop")

    bundle = Bundle(
        id=uuid4(),
        name="Curated",
        slug="curated-bundle",
        visibility="public",
        is_base=False,
        bundle_owner=owner_id,
    )
    db_session.add(bundle)
    db_session.commit()
    # Put artifacts IN the bundle (as the owner).
    db_session.add_all(
        [
            BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source="custom-added"),
            BundlePersonality(bundle_id=bundle.id, personality_id=p.id),
            BundleCompositeLoop(bundle_id=bundle.id, composite_loop_id=cl.id),
        ]
    )
    db_session.commit()

    client = TestClient(_app(db_session, follower_id))
    r = client.post("/api/bundles/curated-bundle/like")
    assert r.status_code == 200, r.text

    # The follower now follows the bundle...
    assert (
        db_session.query(FollowedBundle)
        .filter(FollowedBundle.user_id == follower_id, FollowedBundle.bundle_id == bundle.id)
        .count()
        == 1
    )
    # ...but the follower's Liked bundle has ZERO of the bundle's members.
    # (The follower's Liked bundle is lazily created on first like; it doesn't
    # exist yet because they only followed, never liked a member directly.)
    liked = (
        db_session.query(Bundle)
        .filter(Bundle.bundle_owner == follower_id, Bundle.is_liked.is_(True))
        .first()
    )
    if liked is not None:  # defensive — should not exist
        assert db_session.query(BundleSkill).filter(BundleSkill.bundle_id == liked.id).count() == 0
        assert (
            db_session.query(BundlePersonality).filter(BundlePersonality.bundle_id == liked.id).count()
            == 0
        )
        assert (
            db_session.query(BundleCompositeLoop)
            .filter(BundleCompositeLoop.bundle_id == liked.id)
            .count()
            == 0
        )


# ── GET /api/library populates all shelves + followed bundles ─────────────


def test_library_populated_after_liking_all_types(db_session):
    owner, owner_id = _seed_user(db_session)
    _, other_id = _seed_user(db_session)
    skill = _seed_skill(db_session, "lib-skill")
    p = _seed_personality(db_session, "lib-p")
    cl = _seed_loop(db_session, "lib-loop")
    b = _seed_bundle(db_session, other_id, "lib-bundle")

    client = TestClient(_app(db_session, owner_id))
    assert client.post("/api/skills/lib-skill/like").status_code == 200
    assert client.post("/api/personalities/lib-p/like").status_code == 200
    assert client.post("/api/loops/lib-loop/like").status_code == 200
    assert client.post("/api/bundles/lib-bundle/like").status_code == 200

    r = client.get("/api/library")
    assert r.status_code == 200, r.text
    body = r.json()
    shelves = body["shelves"]
    assert len(shelves["skills"]) == 1
    assert shelves["skills"][0]["slug"] == "lib-skill"
    assert len(shelves["personalities"]) == 1
    assert shelves["personalities"][0]["slug"] == "lib-p"
    assert len(shelves["loops"]) == 1
    assert shelves["loops"][0]["slug"] == "lib-loop"
    assert len(body["followed_bundles"]) == 1
    assert body["followed_bundles"][0]["slug"] == "lib-bundle"


# ── Authz: private personality forbidden ──────────────────────────────────


def test_private_personality_like_forbidden(db_session):
    owner, owner_id = _seed_user(db_session)
    other, other_id = _seed_user(db_session)
    p = Personality(
        slug="secret-soul", title="secret", system_prompt="x", is_public=False, tier="free"
    )
    db_session.add(p)
    db_session.commit()
    client = TestClient(_app(db_session, other_id))
    r = client.post("/api/personalities/secret-soul/like")
    assert r.status_code == 403
    # Nothing landed in the Liked bundle.
    liked = (
        db_session.query(Bundle)
        .filter(Bundle.bundle_owner == other_id, Bundle.is_liked.is_(True))
        .first()
    )
    if liked is not None:
        assert (
            db_session.query(BundlePersonality).filter(BundlePersonality.bundle_id == liked.id).count()
            == 0
        )


# ── Tier gate: free caller cannot like a pro artifact ─────────────────────


def test_tier_gate_blocks_over_tier_personality(db_session):
    owner, owner_id = _seed_user(db_session, tier="free")
    p = Personality(
        slug="pro-soul", title="pro", system_prompt="x", is_public=True, tier="pro"
    )
    db_session.add(p)
    db_session.commit()
    client = TestClient(_app(db_session, owner_id))
    r = client.post("/api/personalities/pro-soul/like")
    assert r.status_code == 403
    assert "tier_gated" in r.text


def test_tier_gate_allows_master(db_session):
    p = Personality(
        slug="pro-soul-2", title="pro", system_prompt="x", is_public=True, tier="pro"
    )
    db_session.add(p)
    db_session.commit()
    uid = uuid4()
    db_session.add(User(id=uid, email=f"m-{uid.hex}@t.test", display_name="m", subscription_tier="free"))
    db_session.commit()

    app = FastAPI()

    def override_get_db():
        yield db_session

    @app.middleware("http")
    async def inject_auth(request: Request, call_next):
        request.state.auth_ctx = AuthContext(
            scope="master", user_id=uid, api_key_id=None, tier="pro_plus"
        )
        return await call_next(request)

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(artifact_like_router)
    client = TestClient(app)
    r = client.post("/api/personalities/pro-soul-2/like")
    assert r.status_code == 200


# ── §0b: provenance badge on federated likes ──────────────────────────────


def test_federated_like_carries_provenance_badge(db_session):
    from app.models import FederationHubSkill, SkillLike

    u, uid = _seed_user(db_session)
    # A federated like (no local skill) with a hub row carrying trust_level.
    db_session.add(
        FederationHubSkill(
            slug="some-fed-skill",
            title="Some Fed Skill",
            upstream_source="clawhub",
            trust_level="community",
        )
    )
    db_session.add(
        SkillLike(user_id=uid, federated_source="clawhub", federated_slug="some-fed-skill")
    )
    db_session.commit()

    client = TestClient(_app(db_session, uid))
    r = client.get("/api/library")
    assert r.status_code == 200
    fed = r.json()["federated_skills"]
    assert len(fed) == 1
    assert fed[0]["slug"] == "some-fed-skill"
    # §0b additive badge — must be present.
    assert fed[0]["provenance"] == "community"


# ── Auth required ─────────────────────────────────────────────────────────


def test_anonymous_like_rejected(db_session):
    _seed_personality(db_session, "anon-target")
    app = FastAPI()

    def override_get_db():
        yield db_session

    @app.middleware("http")
    async def inject_auth(request: Request, call_next):
        request.state.auth_ctx = AuthContext.anonymous()
        return await call_next(request)

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(artifact_like_router)
    client = TestClient(app)
    r = client.post("/api/personalities/anon-target/like")
    assert r.status_code == 401
