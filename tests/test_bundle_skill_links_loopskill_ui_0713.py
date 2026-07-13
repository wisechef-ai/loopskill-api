"""loopskill_ui_0713 — contract test: every bundle-detail skill must carry a
viewable ``source_url`` so a portal visitor can see what they're installing
before clicking anything.

Root cause fixed by this PR: ``public_cookbook_page`` (app/bundle_routes.py)
emitted bare {slug, title, is_public, source, pinned_version} rows with NO
link target — the frontend physically could not make skills clickable.

This test FAILS RED without the serializer change (source_url/loopskill_url
absent from the response) and PASSES GREEN with it.

Also pins the hard "never 404" rule (Adam): ``loopskill_url`` may ONLY be a
``/skills/<slug>`` path (never ``/skills/ext:...`` — that route is verified
404 live for external skills today).
"""

from __future__ import annotations

import re
import uuid
from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Bundle, BundleSkill, Skill, User

_SOURCE_URL_RE = re.compile(r"^https://github\.com/|^https?://|^/skills/")


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _record):
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
    from app.bundle_routes import router as cookbook_router

    app = FastAPI()

    def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.include_router(cookbook_router)
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


def _mk_cookbook(db, owner, slug, name="CB"):
    cb = Bundle(id=uuid.uuid4(), name=name, bundle_owner=owner.id, slug=slug, visibility="public")
    db.add(cb)
    db.commit()
    return cb


def _attach(db, cb, skill, source="custom-added"):
    db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source=source))
    db.commit()


def _mk_external_skill(db, tap_slug, leaf):
    """Mirrors what materialize_external_skill actually writes: is_public=False,
    slug_variant='external', original_source_url = the resolved GitHub tree URL
    (the metasearch/federation layer's per-origin resolver output)."""
    s = Skill(
        id=uuid.uuid4(),
        slug=f"ext:github-marketing:{tap_slug}",
        title=leaf,
        is_public=False,
        skill_variant="external",
        tier="external",
        license="MIT",
        original_source_url=f"https://github.com/coreyhaines31/marketingskills/tree/main/skills/{leaf}",
    )
    db.add(s)
    db.commit()
    return s


def _mk_internal_skill(db, slug):
    s = Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True)
    db.add(s)
    db.commit()
    return s


def test_every_bundle_skill_has_viewable_source_url(db):
    u = _mk_user(db)
    cb = _mk_cookbook(db, u, "link-test-bundle")
    ext = _mk_external_skill(db, "github-marketing--copywriting", "copywriting")
    internal = _mk_internal_skill(db, "summarize-cli")
    _attach(db, cb, ext)
    _attach(db, cb, internal)

    client = TestClient(_public_app(db))
    resp = client.get("/api/cookbooks/public/link-test-bundle")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body["skills"]) == 2
    for sk in body["skills"]:
        assert sk.get("source_url"), f"skill {sk['slug']} missing source_url"
        assert _SOURCE_URL_RE.match(sk["source_url"]), (
            f"source_url {sk['source_url']!r} for {sk['slug']} doesn't match a viewable pattern"
        )
        if sk.get("loopskill_url") is not None:
            assert sk["loopskill_url"].startswith("/skills/"), (
                "loopskill_url must be a real /skills/<slug> page, never /skills/ext:... (404)"
            )
            assert not sk["loopskill_url"].startswith("/skills/ext:")


def test_external_skill_source_url_is_github_and_loopskill_url_is_null(db):
    u = _mk_user(db)
    cb = _mk_cookbook(db, u, "ext-only-bundle")
    ext = _mk_external_skill(db, "github-marketing--ab-testing", "ab-testing")
    _attach(db, cb, ext)

    client = TestClient(_public_app(db))
    resp = client.get("/api/cookbooks/public/ext-only-bundle")
    assert resp.status_code == 200, resp.text
    sk = resp.json()["skills"][0]

    assert sk["source_url"] == (
        "https://github.com/coreyhaines31/marketingskills/tree/main/skills/ab-testing"
    )
    # Hard "never 404" rule: no LoopSkill detail page exists for ext: skills yet.
    assert sk["loopskill_url"] is None


def test_internal_public_skill_gets_a_loopskill_url(db):
    u = _mk_user(db)
    cb = _mk_cookbook(db, u, "internal-only-bundle")
    internal = _mk_internal_skill(db, "summarize-cli")
    _attach(db, cb, internal)

    client = TestClient(_public_app(db))
    resp = client.get("/api/cookbooks/public/internal-only-bundle")
    assert resp.status_code == 200, resp.text
    sk = resp.json()["skills"][0]

    assert sk["loopskill_url"] == "/skills/summarize-cli"
    assert sk["source_url"]
