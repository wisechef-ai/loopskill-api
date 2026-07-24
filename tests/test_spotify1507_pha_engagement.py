"""spotify_1507 Phase A — Engagement tests (likes, favourites, discover, library).

Uses the conftest `db_session` fixture + a lightweight auth-injecting middleware
(mirrors test_liked_0711_p0.py) so the authed engagement routes see a real
user_id on request.state.auth_ctx.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth_ctx import AuthContext
from app.database import get_db
from app.engagement_routes import router as engagement_router
from app.bundle_routes import router as bundle_router
from app.follow_routes import router as follow_router
from app.models import Bundle, Skill, SkillFavourite, SkillLike, User


def _app(db, user_id):
    """App with engagement + bundle + follow routers and an injected user ctx."""
    app = FastAPI()

    def override_get_db():
        yield db

    @app.middleware("http")
    async def inject_auth(request: Request, call_next):
        request.state.auth_ctx = AuthContext(scope="user", user_id=user_id, api_key_id=None, tier="free")
        return await call_next(request)

    app.dependency_overrides[get_db] = override_get_db
    app.include_router(engagement_router)
    app.include_router(bundle_router)
    app.include_router(follow_router)
    return app


def _seed_user(db, email=None):
    uid = uuid4()
    email = email or f"user-{uid.hex[:8]}@test.com"
    db.add(User(id=uid, email=email, display_name="Test User", subscription_tier="free"))
    db.commit()
    return uid


def _seed_skill(db, slug):
    skill = Skill(slug=slug, title=slug, description=f"Test {slug}", tier="free", kind="skill")
    db.add(skill)
    db.commit()
    return skill


# ── Likes ──────────────────────────────────────────────────────────────────


def test_like_local_skill_persists_and_counts(db_session):
    uid = _seed_user(db_session)
    _seed_skill(db_session, "memory")
    client = TestClient(_app(db_session, uid))

    r = client.post("/api/skills/memory/like")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["liked"] is True
    assert body["like_count"] == 1


def test_like_is_idempotent(db_session):
    uid = _seed_user(db_session)
    _seed_skill(db_session, "arxiv")
    client = TestClient(_app(db_session, uid))

    client.post("/api/skills/arxiv/like")
    r = client.post("/api/skills/arxiv/like")  # second like — must not double-count
    assert r.status_code == 200
    assert r.json()["like_count"] == 1


def test_unlike_removes(db_session):
    uid = _seed_user(db_session)
    _seed_skill(db_session, "dspy")
    client = TestClient(_app(db_session, uid))

    client.post("/api/skills/dspy/like")
    r = client.delete("/api/skills/dspy/like")
    assert r.status_code == 200
    assert r.json()["liked"] is False
    assert r.json()["like_count"] == 0


def test_like_federated_track(db_session):
    """A federated track (source__slug identity) is likeable even with no local skill."""
    uid = _seed_user(db_session)
    client = TestClient(_app(db_session, uid))

    # clawhub__some-federated-skill → federated_source='clawhub', slug='some-federated-skill'
    r = client.post("/api/skills/clawhub__some-federated-skill/like")
    assert r.status_code == 200, r.text
    assert r.json()["liked"] is True
    assert r.json()["like_count"] == 1

    # verify it persisted with federated identity
    row = db_session.query(SkillLike).filter(SkillLike.federated_source == "clawhub").first()
    assert row is not None
    assert row.federated_slug == "some-federated-skill"
    assert row.skill_id is None


def test_like_unknown_slug_404(db_session):
    uid = _seed_user(db_session)
    client = TestClient(_app(db_session, uid))
    r = client.post("/api/skills/nonexistent-no-double-underscore/like")
    assert r.status_code == 404


# ── Favourites ───────────────────────────────────────────────────────────────


def test_favourite_and_unfavourite(db_session):
    uid = _seed_user(db_session)
    _seed_skill(db_session, "notion")
    client = TestClient(_app(db_session, uid))

    r = client.post("/api/skills/notion/favourite")
    assert r.status_code == 200
    assert r.json()["favourited"] is True
    assert db_session.query(SkillFavourite).count() == 1

    r = client.delete("/api/skills/notion/favourite")
    assert r.status_code == 200
    assert r.json()["favourited"] is False
    assert db_session.query(SkillFavourite).count() == 0


# ── Discover (engagement sort) ───────────────────────────────────────────────


def test_discover_engagement_sort_by_followers(db_session):
    """discover?sort=engagement ranks public bundles by follower_count, editorial first."""
    owner = _seed_user(db_session)
    b_low = Bundle(
        id=uuid4(),
        name="Low Followers",
        visibility="public",
        is_base=False,
        slug="low-followers",
        follower_count=2,
        bundle_owner=owner,
    )
    b_high = Bundle(
        id=uuid4(),
        name="High Followers",
        visibility="public",
        is_base=False,
        slug="high-followers",
        follower_count=50,
        bundle_owner=owner,
    )
    b_editorial = Bundle(
        id=uuid4(),
        name="Editorial Pick",
        visibility="public",
        is_base=False,
        slug="editorial-pick",
        follower_count=5,
        is_editorial=True,
        curated_by="human",
        bundle_owner=owner,
    )
    b_private = Bundle(
        id=uuid4(),
        name="Private",
        visibility="private",
        is_base=False,
        slug="private-one",
        follower_count=999,
        bundle_owner=owner,
    )
    db_session.add_all([b_low, b_high, b_editorial, b_private])
    db_session.commit()

    client = TestClient(_app(db_session, owner))
    r = client.get("/api/bundles/discover?sort=engagement")
    assert r.status_code == 200, r.text
    cards = r.json()["cookbooks"]
    names = [c["name"] for c in cards]
    # private excluded
    assert "Private" not in names
    # editorial floats to top
    assert names[0] == "Editorial Pick"
    # then by follower_count desc among non-editorial
    assert names.index("High Followers") < names.index("Low Followers")
    # new fields surfaced
    ed = next(c for c in cards if c["name"] == "Editorial Pick")
    assert ed["is_editorial"] is True
    assert ed["curated_by"] == "human"
    assert ed["follower_count"] == 5


def test_follower_count_maintained_on_follow(db_session):
    """Following a bundle increments its denormalized follower_count."""
    owner = _seed_user(db_session)
    follower = _seed_user(db_session)
    bundle = Bundle(
        id=uuid4(),
        name="Followable",
        visibility="public",
        is_base=False,
        slug="followable",
        follower_count=0,
        bundle_owner=owner,
    )
    db_session.add(bundle)
    db_session.commit()

    client = TestClient(_app(db_session, follower))
    r = client.post(f"/api/bundles/{bundle.id}/follow")
    assert r.status_code == 200, r.text
    db_session.refresh(bundle)
    assert bundle.follower_count == 1

    client.delete(f"/api/bundles/{bundle.id}/follow")
    db_session.refresh(bundle)
    assert bundle.follower_count == 0


# ── My Library ───────────────────────────────────────────────────────────────


def test_my_library_aggregates(db_session):
    uid = _seed_user(db_session)
    skill = _seed_skill(db_session, "grok-search")
    owned = Bundle(
        id=uuid4(),
        name="My Bundle",
        visibility="private",
        is_base=False,
        slug="my-bundle",
        bundle_owner=uid,
    )
    db_session.add(owned)
    db_session.commit()

    client = TestClient(_app(db_session, uid))
    client.post("/api/skills/grok-search/like")
    client.post("/api/skills/grok-search/favourite")

    r = client.get("/api/me/library")
    assert r.status_code == 200, r.text
    lib = r.json()
    assert len(lib["likes"]) == 1
    assert len(lib["favourites"]) == 1
    assert len(lib["owned_bundles"]) == 1
    assert lib["owned_bundles"][0]["name"] == "My Bundle"


# ── Typed tracks ─────────────────────────────────────────────────────────────


def test_valid_skill_kinds_extended():
    from app.models import VALID_SKILL_KINDS

    assert "mcp-server" in VALID_SKILL_KINDS
    assert "personality" in VALID_SKILL_KINDS
    assert "skill" in VALID_SKILL_KINDS
    assert "loop" in VALID_SKILL_KINDS
