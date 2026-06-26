"""Tests for spotify_0608 Ph B — public cookbook discovery + install-count integrity.

Covers:
  - is_test install-count integrity (§4.2): test-keyed installs excluded from
    _install_counts_for AND from the denormalized Skill.install_count bump
  - GET /api/cookbooks/discover (public, ranked, paginated, public-only)
  - GET /api/cookbooks/public/{slug} (public page, 404 on private, ?ref + clone_line)
"""
from __future__ import annotations

import uuid
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import (
    APIKey,
    Base,
    Bundle,
    BundleSkill,
    InstallEvent,
    Skill,
    User,
)


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
def db(engine_fixture) -> Generator[Session, None, None]:
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


def _public_app(db: Session) -> FastAPI:
    """App with just the cookbook router; no auth middleware (routes are public)."""
    from app.bundle_routes import router as cookbook_router

    app = FastAPI()

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.include_router(cookbook_router)
    return app


def _mk_user(db, tier="pro"):
    u = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int) % 1_000_000_000,
        email=f"u-{uuid.uuid4().hex[:6]}@t.io",
        display_name="u",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.commit()
    return u


def _mk_cookbook(db, owner, slug, visibility="public", name="CB"):
    cb = Bundle(
        id=uuid.uuid4(),
        name=name,
        bundle_owner=owner.id,
        slug=slug,
        visibility=visibility,
    )
    db.add(cb)
    db.commit()
    return cb


def _mk_skill(db, slug):
    s = Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True)
    db.add(s)
    db.commit()
    return s


def _attach(db, cb, skill, source="custom-added"):
    db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source=source))
    db.commit()


def _install(db, skill, api_key_id=None, cookbook_id=None):
    db.add(InstallEvent(id=uuid.uuid4(), skill_id=skill.id, skill_slug=skill.slug,
                        api_key_id=api_key_id, bundle_id=cookbook_id))
    db.commit()


# ── §4.2 install-count integrity ──────────────────────────────────────────


def test_install_counts_exclude_test_keyed(db):
    from app._skill_helpers import _install_counts_for

    u = _mk_user(db)
    s = _mk_skill(db, "counted")
    # one organic-anon, one organic-key, one test-key
    organic_key = APIKey(id=uuid.uuid4(), user_id=u.id, key_prefix="rec_a", key_hash="h", is_test=False)
    test_key = APIKey(id=uuid.uuid4(), user_id=u.id, key_prefix="rec_t", key_hash="h", is_test=True)
    db.add_all([organic_key, test_key])
    db.commit()

    _install(db, s, api_key_id=None)            # anon → organic
    _install(db, s, api_key_id=organic_key.id)  # organic
    _install(db, s, api_key_id=test_key.id)     # test → excluded
    _install(db, s, api_key_id=test_key.id)     # test → excluded

    counts = _install_counts_for(db, [s.id])
    total, _7d = counts[s.id]
    assert total == 2, f"expected 2 organic, got {total}"


def test_record_install_event_skips_count_bump_for_test_key(db):
    from app._skill_helpers import _record_install_event

    u = _mk_user(db)
    s = _mk_skill(db, "bumpcheck")
    assert (s.install_count or 0) == 0
    test_key = APIKey(id=uuid.uuid4(), user_id=u.id, key_prefix="rec_t", key_hash="h", is_test=True)
    organic_key = APIKey(id=uuid.uuid4(), user_id=u.id, key_prefix="rec_o", key_hash="h", is_test=False)
    db.add_all([test_key, organic_key])
    db.commit()

    class _Req:
        def __init__(self, kid):
            self.state = type("S", (), {"api_key_id": kid})()

    _record_install_event(db, skill=s, version_semver="1.0.0", request=_Req(test_key.id))
    db.commit()
    db.refresh(s)
    assert (s.install_count or 0) == 0, "test-keyed install must NOT bump install_count"

    _record_install_event(db, skill=s, version_semver="1.0.0", request=_Req(organic_key.id))
    db.commit()
    db.refresh(s)
    assert s.install_count == 1, "organic install must bump install_count"

    # InstallEvent rows ALWAYS written (audit), regardless of is_test
    assert db.query(InstallEvent).filter(InstallEvent.skill_id == s.id).count() == 2


# ── discover endpoint ─────────────────────────────────────────────────────


def test_discover_returns_only_public(db):
    u = _mk_user(db)
    pub = _mk_cookbook(db, u, "pub-one", visibility="public", name="Public One")
    _mk_cookbook(db, u, "priv-one", visibility="private", name="Private One")
    client = TestClient(_public_app(db))
    resp = client.get("/api/cookbooks/discover")
    assert resp.status_code == 200, resp.text
    slugs = [c["slug"] for c in resp.json()["cookbooks"]]
    assert "pub-one" in slugs
    assert "priv-one" not in slugs


def test_discover_ranks_by_real_installs_excluding_test(db):
    u = _mk_user(db)
    test_key = APIKey(id=uuid.uuid4(), user_id=u.id, key_prefix="rec_t", key_hash="h", is_test=True)
    db.add(test_key)
    db.commit()

    cb_low = _mk_cookbook(db, u, "low", name="Low")
    cb_high = _mk_cookbook(db, u, "high", name="High")
    s_low = _mk_skill(db, "s-low")
    s_high = _mk_skill(db, "s-high")
    _attach(db, cb_low, s_low)
    _attach(db, cb_high, s_high)

    # portal_0610 R7: installs are attributed to the cookbook they came THROUGH
    # (InstallEvent.cookbook_id), not summed from each skill's global count.
    # low gets 5 TEST installs (should not count); high gets 2 organic.
    for _ in range(5):
        _install(db, s_low, api_key_id=test_key.id, cookbook_id=cb_low.id)
    _install(db, s_high, api_key_id=None, cookbook_id=cb_high.id)
    _install(db, s_high, api_key_id=None, cookbook_id=cb_high.id)

    client = TestClient(_public_app(db))
    resp = client.get("/api/cookbooks/discover?sort=installs")
    assert resp.status_code == 200, resp.text
    cards = {c["slug"]: c for c in resp.json()["cookbooks"]}
    assert cards["high"]["installs_total"] == 2
    assert cards["low"]["installs_total"] == 0
    # high ranks first
    assert resp.json()["cookbooks"][0]["slug"] == "high"


def test_discover_pagination(db):
    u = _mk_user(db)
    for i in range(5):
        _mk_cookbook(db, u, f"cb-{i}", name=f"CB{i}")
    client = TestClient(_public_app(db))
    resp = client.get("/api/cookbooks/discover?sort=newest&limit=2&offset=0")
    assert resp.status_code == 200
    assert len(resp.json()["cookbooks"]) == 2


# ── public cookbook page ──────────────────────────────────────────────────


def test_public_page_renders_with_ref_and_clone_line(db):
    u = _mk_user(db)
    cb = _mk_cookbook(db, u, "awakened", name="The Awakened Agent")
    s = _mk_skill(db, "summarize-cli")
    _attach(db, cb, s)
    client = TestClient(_public_app(db))
    resp = client.get("/api/cookbooks/public/awakened")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slug"] == "awakened"
    assert body["ref"] == str(u.id)
    assert "summarize-cli" in [sk["slug"] for sk in body["skills"]]
    assert "cookbook://awakened" in body["clone_line"]
    assert f"?ref={u.id}" in body["clone_line"]


def test_public_page_404_for_private(db):
    u = _mk_user(db)
    _mk_cookbook(db, u, "secret", visibility="private")
    client = TestClient(_public_app(db))
    resp = client.get("/api/cookbooks/public/secret")
    assert resp.status_code == 404


def test_public_page_404_for_unknown(db):
    client = TestClient(_public_app(db))
    resp = client.get("/api/cookbooks/public/does-not-exist")
    assert resp.status_code == 404
