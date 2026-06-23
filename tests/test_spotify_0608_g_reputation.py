"""Tests for spotify_0608 Ph G — reputation surfaces.

Covers:
  - cookbooks.is_verified column + verified-maintainer badge on public cards
  - GET /api/cookbooks/leaderboard (top_weekly by real installs, latest by
    created_at; public; test/CI installs excluded)
  - POST /api/cookbooks/{id}/verify (master-only assign/revoke)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Cookbook, CookbookSkill, InstallEvent, Skill, User


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(engine_fixture) -> Generator[Session, None, None]:
    conn = engine_fixture.connect()
    tx = conn.begin()
    SessionLocal = sessionmaker(bind=conn, autocommit=False, autoflush=False)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
        tx.rollback()
        conn.close()


def _app(db, *, master=False, user_id=None):
    """App with the cookbook router. master=True simulates the admin key
    (api_key_user_id=None). Pass user_id to simulate an authenticated non-master
    user (so master-only routes reach their 403 rather than a 401)."""
    from app.bundle_routes import router

    app = FastAPI()

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db

    @app.middleware("http")
    async def _stamp(request, call_next):
        # require_cookbook_tier reads these off request.state.
        if master:
            request.state.api_key_user_id = None
        elif user_id is not None:
            request.state.api_key_user_id = user_id
        else:
            request.state.api_key_user_id = "MISSING"
        request.state.is_cbt_token = False
        return await call_next(request)

    app.include_router(router)
    return app


def _mk_user(db):
    u = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int) % 1_000_000_000,
        email=f"u-{uuid.uuid4().hex[:6]}@t.io",
        display_name="u",
        subscription_tier="pro",
        subscription_status="active",
    )
    db.add(u)
    db.commit()
    return u


def _mk_cb(db, owner, slug, *, visibility="public", verified=False, created=None):
    cb = Cookbook(
        id=uuid.uuid4(),
        name=slug,
        bundle_owner=owner.id,
        slug=slug,
        visibility=visibility,
        is_verified=verified,
    )
    if created:
        cb.created_at = created
    db.add(cb)
    db.commit()
    return cb


def _mk_skill(db, slug):
    s = Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True, install_count=0)
    db.add(s)
    db.commit()
    return s


def _attach(db, cb, skill):
    db.add(CookbookSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
    db.commit()


def _install(db, skill, *, days_ago=0, n=1, api_key_id=None):
    for _ in range(n):
        ev = InstallEvent(
            id=uuid.uuid4(),
            skill_id=skill.id,
            skill_slug=skill.slug,
            version_semver="1.0.0",
            api_key_id=api_key_id,
        )
        ev.created_at = datetime.utcnow() - timedelta(days=days_ago)
        db.add(ev)
    db.commit()


# ── model ────────────────────────────────────────────────────────────────────


def test_is_verified_defaults_false(db):
    owner = _mk_user(db)
    cb = _mk_cb(db, owner, "plain")
    assert cb.is_verified is False


# ── leaderboard ────────────────────────────────────────────────────────────


def test_leaderboard_top_weekly_ranks_by_7d_installs(db):
    owner = _mk_user(db)
    cb_hot = _mk_cb(db, owner, "hot")
    cb_cold = _mk_cb(db, owner, "cold")
    s_hot = _mk_skill(db, "s-hot")
    _attach(db, cb_hot, s_hot)
    s_cold = _mk_skill(db, "s-cold")
    _attach(db, cb_cold, s_cold)
    _install(db, s_hot, days_ago=1, n=5)  # within 7d
    _install(db, s_cold, days_ago=30, n=5)  # outside 7d

    with TestClient(_app(db)) as c:
        r = c.get("/api/cookbooks/leaderboard")
    assert r.status_code == 200
    body = r.json()
    assert "top_weekly" in body and "latest" in body
    slugs = [e["slug"] for e in body["top_weekly"]]
    assert slugs[0] == "hot"  # 5 installs in the 7d window beats the cold one


def test_leaderboard_latest_ranks_by_created_at(db):
    owner = _mk_user(db)
    _mk_cb(db, owner, "older", created=datetime.utcnow() - timedelta(days=10))
    _mk_cb(db, owner, "newer", created=datetime.utcnow() - timedelta(days=1))
    with TestClient(_app(db)) as c:
        body = c.get("/api/cookbooks/leaderboard").json()
    latest_slugs = [e["slug"] for e in body["latest"]]
    assert latest_slugs[0] == "newer"


def test_leaderboard_excludes_private(db):
    owner = _mk_user(db)
    _mk_cb(db, owner, "pub", visibility="public")
    _mk_cb(db, owner, "priv", visibility="private")
    with TestClient(_app(db)) as c:
        body = c.get("/api/cookbooks/leaderboard").json()
    all_slugs = {e["slug"] for e in body["latest"]}
    assert "pub" in all_slugs
    assert "priv" not in all_slugs


def test_leaderboard_card_carries_is_verified(db):
    owner = _mk_user(db)
    _mk_cb(db, owner, "verif", verified=True)
    with TestClient(_app(db)) as c:
        body = c.get("/api/cookbooks/leaderboard").json()
    card = next(e for e in body["latest"] if e["slug"] == "verif")
    assert card["is_verified"] is True


# ── verify endpoint ──────────────────────────────────────────────────────────


def test_verify_requires_master(db):
    owner = _mk_user(db)
    cb = _mk_cb(db, owner, "tv", verified=False)
    # Authenticated NON-master user → reaches the master gate → 403 (not 401).
    with TestClient(_app(db, user_id=owner.id)) as c:
        r = c.post(f"/api/cookbooks/{cb.id}/verify")
    assert r.status_code == 403


def test_verify_master_sets_and_revokes(db):
    owner = _mk_user(db)
    cb = _mk_cb(db, owner, "tv2", verified=False)
    with TestClient(_app(db, master=True)) as c:
        r1 = c.post(f"/api/cookbooks/{cb.id}/verify")
        assert r1.status_code == 200
        assert r1.json()["is_verified"] is True
        r2 = c.post(f"/api/cookbooks/{cb.id}/verify?verified=false")
        assert r2.json()["is_verified"] is False


def test_verify_404_unknown(db):
    with TestClient(_app(db, master=True)) as c:
        r = c.post(f"/api/cookbooks/{uuid.uuid4()}/verify")
    assert r.status_code == 404


def test_public_card_is_verified_flag_on_discover(db):
    owner = _mk_user(db)
    _mk_cb(db, owner, "disc-verif", verified=True)
    with TestClient(_app(db)) as c:
        body = c.get("/api/cookbooks/discover").json()
    card = next(e for e in body["cookbooks"] if e["slug"] == "disc-verif")
    assert card["is_verified"] is True
