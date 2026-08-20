"""RED-proof regression for issue #155 — Skill.title unpopulated / regresses.

Two behaviours locked in:
1. Publishing a NEW skill whose manifest `name` equals its slug (no real
   title set by the creator) must NOT create a title == slug row — it must
   derive a display title (slug -> Title Case fallback, since no SKILL.md
   frontmatter is present in these fixtures).
2. RE-publishing an EXISTING skill that already has a good editorial title
   must NOT regress that title back to the raw slug just because the new
   manifest's `name` field happens to equal the slug. This was a live bug
   in the pre-fix re-sync block (`new_title = skill_name; if new_title !=
   existing: skill_obj.title = new_title` unconditionally accepted a
   slug-shaped title).

On pre-fix `main`, both assertions in this file FAIL (title == slug in both
cases). Verified below in the PR body's Breaker report.
"""

from __future__ import annotations

import hashlib
import io
import os
from typing import Generator
from unittest.mock import patch
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Creator, Skill, User


@pytest.fixture(scope="session")
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


def _make_user(db: Session) -> User:
    uid = uuid4()
    user = User(id=uid, display_name="Test Creator", email=f"{uid}@test.example")
    db.add(user)
    db.flush()
    return user


def _make_creator(db: Session, user: User, slug="test-creator-155") -> Creator:
    creator = Creator(id=uuid4(), user_id=user.id, name="Test Creator", slug=slug)
    db.add(creator)
    db.flush()
    return creator


def _make_keypair():
    priv = Ed25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes_raw()
    return priv, pub_bytes


def _sign_tarball(priv_key: Ed25519PrivateKey, tarball_bytes: bytes) -> bytes:
    digest = hashlib.sha256(tarball_bytes).digest()
    return priv_key.sign(digest)


def _valid_toml(name: str, slug: str, version="1.0.0") -> bytes:
    return (
        f"[skill]\n"
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        f'description = "A test skill"\n'
        f'license = "MIT"\n'
        f'entrypoint = "run.sh"\n'
        f'slug = "{slug}"\n'
    ).encode()


def _make_client(db: Session, skills_dir: str, api_key_user_id):
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest

    from app.publisher_routes import router as publisher_router

    test_app = FastAPI()

    def _override_get_db():
        try:
            yield db
        finally:
            pass

    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.include_router(publisher_router)

    _uid = api_key_user_id

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next):
            request.state.api_key_user_id = _uid
            request.state.api_key_id = None
            return await call_next(request)

    test_app.add_middleware(InjectAuthState)

    env_patch = patch.dict(os.environ, {"RECIPES_SKILLS_DIR": skills_dir})
    env_patch.start()

    client = TestClient(test_app, raise_server_exceptions=True)
    return client, env_patch


def _publish(client, name, slug, version="1.0.0"):
    priv, pub_bytes = _make_keypair()
    tarball_bytes = f"fake tarball for {slug}@{version}".encode()
    sig_bytes = _sign_tarball(priv, tarball_bytes)
    toml_bytes = _valid_toml(name=name, slug=slug, version=version)
    return client.post(
        "/api/skills/_publish",
        files={
            "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
            "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
            "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
            "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
        },
        data={"is_public": "true"},
        headers={"x-api-key": "rec_test_key"},
    )


class TestIssue155NewSkillNeverTitleLess:
    def test_new_skill_slug_shaped_name_gets_derived_title(self, db_session, tmp_path):
        """A creator publishing with manifest name == slug must NOT get
        title == slug on the created row."""
        user = _make_user(db_session)
        _make_creator(db_session, user)
        db_session.commit()

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = _publish(client, name="gh-fix-ci", slug="gh-fix-ci")
        finally:
            env.stop()

        assert resp.status_code == 201, resp.text
        skill = db_session.query(Skill).filter(Skill.slug == "gh-fix-ci").first()
        assert skill is not None
        assert skill.title != skill.slug, f"title regressed to slug: {skill.title!r}"
        # slug_to_title CLI-tool preservation: 'gh' stays lowercase; 'ci' is
        # a known acronym (see app/skill_title.py ACRONYMS) so it uppercases.
        assert skill.title == "gh Fix CI"


class TestIssue155RepublishDoesNotRegressGoodTitle:
    def test_republish_with_slug_shaped_name_keeps_existing_good_title(self, db_session, tmp_path):
        """Regression: re-publishing an existing skill whose row already has
        a real editorial title must not overwrite it back to the slug just
        because the manifest's `name` field is slug-shaped this time."""
        user = _make_user(db_session)
        creator = _make_creator(db_session, user)
        skill = Skill(
            id=uuid4(),
            slug="audiocraft",
            title="AudioCraft",  # good editorial title, pre-existing
            description="A test skill",
            license="MIT",
            is_public=True,
            creator_id=creator.id,
        )
        db_session.add(skill)
        db_session.commit()

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            # Republish with name == slug (e.g. CI pipeline regenerated
            # skill.toml without the human-authored title this time).
            resp = _publish(client, name="audiocraft", slug="audiocraft", version="1.0.1")
        finally:
            env.stop()

        assert resp.status_code == 201, resp.text
        db_session.refresh(skill)
        assert skill.title == "AudioCraft", f"good title regressed to slug-shaped value: {skill.title!r}"
