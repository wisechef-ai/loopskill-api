"""Tests for POST /api/skills/_publish — WIS-SP2.

Tests cover (8+ required):
1. success — happy path returns {skill_id, version, tarball_path, sha256}
2. missing_skill_toml — 422 when skill_toml file is empty
3. invalid_signature — 400 when ed25519 sig doesn't verify
4. wrong_creator — 403 when api_key user ≠ skill's creator
5. version_exists — 409 when re-publishing same (skill_id, semver)
6. missing_license — 422 when skill.toml lacks [skill].license
7. oversized_tarball — 413 when tarball > 10 MB
8. private_vs_public_visibility — private skill hidden from /api/skills/search

Setup uses in-memory SQLite + TestClient (no real DB / Redis needed).
The APIKeyMiddleware is bypassed by patching request.state.api_key_user_id directly
via a middleware override in the test app factory.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path
from typing import Generator
from unittest.mock import patch
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# ── Project root on sys.path (set by PYTHONPATH=. in pytest invocation) ─
from app.database import get_db
from app.models import Base, Creator, Skill, SkillVersion, User


# ─────────────────────────── DB Fixtures ────────────────────────────────

@pytest.fixture(scope="session")
def engine_fixture():
    """In-memory SQLite engine shared for the test session."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite doesn't enforce FK by default — enable it
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture) -> Generator[Session, None, None]:
    """Per-test transactional session that rolls back after each test."""
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


# ─────────────────────────── Helpers ─────────────────────────────────────

def _make_user(db: Session, user_id=None) -> User:
    uid = user_id or uuid4()
    user = User(id=uid, display_name="Test Creator", email=f"{uid}@test.example")
    db.add(user)
    db.flush()
    return user


def _make_creator(db: Session, user: User, slug="test-creator") -> Creator:
    creator = Creator(id=uuid4(), user_id=user.id, name="Test Creator", slug=slug)
    db.add(creator)
    db.flush()
    return creator


def _make_skill(db: Session, creator: Creator, slug="my-skill", is_public=False) -> Skill:
    skill = Skill(
        id=uuid4(),
        slug=slug,
        title="My Skill",
        description="A test skill",
        license="MIT",
        is_public=is_public,
        creator_id=creator.id,
    )
    db.add(skill)
    db.flush()
    return skill


def _make_keypair():
    """Return (private_key, public_key_bytes_raw)."""
    priv = Ed25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes_raw()
    return priv, pub_bytes


def _sign_tarball(priv_key: Ed25519PrivateKey, tarball_bytes: bytes) -> bytes:
    """Sign sha256(tarball) with ed25519 private key."""
    digest = hashlib.sha256(tarball_bytes).digest()
    return priv_key.sign(digest)


def _valid_toml(
    name="my-skill",
    version="1.0.0",
    description="A test skill",
    license="MIT",
    entrypoint="run.sh",
    slug=None,
    extra: dict | None = None,
) -> bytes:
    """Build a minimal valid skill.toml bytes."""
    slug_line = f'\nslug = "{slug}"' if slug else ""
    extra_lines = ""
    if extra:
        for k, v in extra.items():
            extra_lines += f'\n{k} = "{v}"'
    return (
        f'[skill]\n'
        f'name = "{name}"\n'
        f'version = "{version}"\n'
        f'description = "{description}"\n'
        f'license = "{license}"\n'
        f'entrypoint = "{entrypoint}"'
        f'{slug_line}'
        f'{extra_lines}\n'
    ).encode()


def _make_tarball(content: bytes = b"fake tarball data") -> bytes:
    return content


# ─────────────────────────── App Fixture ─────────────────────────────────

def _make_client(db: Session, skills_dir: str, api_key_user_id=None, is_admin=False):
    """Create a TestClient with a minimal test app (no APIKeyMiddleware or Redis):
    - Includes only the routes needed: publisher_router + router (for search)
    - Injects request.state.api_key_user_id directly via a test middleware
    - DB session overridden to our in-memory SQLite session
    - Tarball storage pointed at a temp dir
    """
    from fastapi import FastAPI
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request as StarletteRequest

    from app.publisher_routes import router as publisher_router
    from app.routes import router as skills_router

    test_app = FastAPI()

    # Override the DB dependency
    def _override_get_db():
        try:
            yield db
        finally:
            pass

    test_app.dependency_overrides[get_db] = _override_get_db
    test_app.include_router(skills_router)
    test_app.include_router(publisher_router)

    # Inject auth state (simulating what APIKeyMiddleware would set)
    _uid = api_key_user_id

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request: StarletteRequest, call_next):
            # is_admin → api_key_user_id = None (same as master key path)
            request.state.api_key_user_id = None if is_admin else _uid
            request.state.api_key_id = None
            return await call_next(request)

    test_app.add_middleware(InjectAuthState)

    env_patch = patch.dict(os.environ, {"RECIPES_SKILLS_DIR": skills_dir})
    env_patch.start()

    client = TestClient(test_app, raise_server_exceptions=True)
    return client, env_patch


# ─────────────────────────── Test Cases ──────────────────────────────────

class TestPublishSkillSuccess:
    """AC1 — happy path returns {skill_id, version, tarball_path, sha256}."""

    def test_publish_new_skill_success(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="pub-creator-1")
        skill = _make_skill(db_session, creator, slug="new-skill-1")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = _make_tarball(b"real tarball content here")
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(name="new-skill-1", slug="new-skill-1", version="0.1.0")

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                data={"is_public": "false"},
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert "skill_id" in body
        assert body["version"] == "0.1.0"
        assert "tarball_path" in body
        assert "sha256" in body
        expected_sha = hashlib.sha256(tarball_bytes).hexdigest()
        assert body["sha256"] == expected_sha

    def test_publish_creates_tarball_on_disk(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="pub-creator-2")
        skill = _make_skill(db_session, creator, slug="disk-skill")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"disk tarball content"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(name="disk-skill", slug="disk-skill", version="1.2.3")

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 201, resp.text
        tarball_path = resp.json()["tarball_path"]
        assert Path(tarball_path).exists()
        assert Path(tarball_path).read_bytes() == tarball_bytes
        # Check mode 0640
        mode = oct(Path(tarball_path).stat().st_mode)[-4:]
        assert mode == "0640", f"Expected 0640, got {mode}"


class TestPublishMissingSkillToml:
    """AC9 — missing skill_toml → 422."""

    def test_empty_skill_toml_rejected(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="pub-creator-3")
        skill = _make_skill(db_session, creator, slug="toml-skill")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        empty_toml = b""

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(empty_toml), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 422, resp.text
        assert "skill_toml" in resp.text.lower() or "required" in resp.text.lower()


class TestPublishInvalidSignature:
    """AC3 — invalid ed25519 signature → 400 'invalid_signature'."""

    def test_wrong_signature_rejected(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="pub-creator-4")
        skill = _make_skill(db_session, creator, slug="sig-skill")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"tarball content"
        # Sign different data — wrong signature
        sig_bytes = _sign_tarball(priv, b"wrong data")
        toml_bytes = _valid_toml(name="sig-skill", slug="sig-skill", version="1.0.0")

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 400, resp.text
        assert "invalid_signature" in resp.text

    def test_garbage_pubkey_rejected(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="pub-creator-4b")
        skill = _make_skill(db_session, creator, slug="sig-skill-2")
        db_session.commit()

        priv, _pub_bytes = _make_keypair()
        tarball_bytes = b"tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        bad_pubkey = b"\x00" * 16  # wrong length
        toml_bytes = _valid_toml(name="sig-skill-2", slug="sig-skill-2", version="1.0.0")

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(bad_pubkey), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 400, resp.text
        assert "invalid_signature" in resp.text


class TestPublishWrongCreator:
    """AC2 — wrong creator (api_key user ≠ skill creator) → 403."""

    def test_wrong_creator_rejected(self, db_session, tmp_path):
        # Create the skill owner
        owner = _make_user(db_session)
        creator = _make_creator(db_session, owner, slug="real-creator")
        skill = _make_skill(db_session, creator, slug="owner-skill")
        db_session.commit()

        # A different user tries to publish
        other_user = _make_user(db_session)
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(name="owner-skill", slug="owner-skill", version="1.0.0")

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=other_user.id)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 403, resp.text

    def test_admin_master_key_can_publish_any_skill(self, db_session, tmp_path):
        owner = _make_user(db_session)
        creator = _make_creator(db_session, owner, slug="admin-test-creator")
        skill = _make_skill(db_session, creator, slug="admin-skill")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"admin tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(name="admin-skill", slug="admin-skill", version="9.9.9")

        # is_admin=True → api_key_user_id=None (master key)
        client, env = _make_client(db_session, str(tmp_path), is_admin=True)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 201, resp.text


class TestPublishVersionExists:
    """AC4 — re-publishing same (skill_id, semver) → 409 version_exists."""

    def test_duplicate_version_returns_409(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="dup-creator")
        skill = _make_skill(db_session, creator, slug="dup-skill")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"first publish"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(name="dup-skill", slug="dup-skill", version="1.0.0")

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            # First publish — succeeds
            resp1 = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
            assert resp1.status_code == 201, resp1.text

            # Second publish — same version → 409
            resp2 = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp2.status_code == 409, resp2.text
        assert "version_exists" in resp2.text


class TestPublishMissingLicense:
    """AC7/AC9 — skill.toml missing required field 'license' → 422."""

    def test_missing_license_field_rejected(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="lic-creator")
        skill = _make_skill(db_session, creator, slug="lic-skill")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        # Toml missing 'license'
        bad_toml = b'[skill]\nname = "lic-skill"\nversion = "1.0.0"\ndescription = "test"\nentrypoint = "run.sh"\n'

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(bad_toml), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 422, resp.text
        body = resp.text
        assert "license" in body


class TestPublishOversizedTarball:
    """AC9 — tarball > 10 MB → 413."""

    def test_oversized_tarball_rejected(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="big-creator")
        skill = _make_skill(db_session, creator, slug="big-skill")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        # 10 MB + 1 byte
        big_tarball = b"x" * (10 * 1024 * 1024 + 1)
        sig_bytes = _sign_tarball(priv, big_tarball)
        toml_bytes = _valid_toml(name="big-skill", slug="big-skill", version="1.0.0")

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(big_tarball), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 413, resp.text
        assert "10 MB" in resp.text or "10485760" in resp.text or "maximum" in resp.text.lower()


class TestPublishPrivateVsPublicVisibility:
    """AC5 — is_public=false skills hidden from /api/skills/search."""

    def test_private_skill_not_in_search(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="vis-creator")
        # Publish as private (default)
        skill = _make_skill(db_session, creator, slug="private-vis-skill", is_public=False)
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"private tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(
            name="private-vis-skill", slug="private-vis-skill", version="1.0.0"
        )

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                data={"is_public": "false"},
                headers={"x-api-key": "rec_test_key"},
            )
            assert resp.status_code == 201, resp.text

            # Now search — should NOT appear (is_public=False)
            search_resp = client.get(
                "/api/skills/search",
                params={"q": "private-vis-skill"},
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert search_resp.status_code == 200, search_resp.text
        body = search_resp.json()
        slugs = [r["slug"] for r in body.get("results", [])]
        assert "private-vis-skill" not in slugs, f"Private skill appeared in search: {slugs}"

    def test_public_skill_appears_in_search(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="pub-vis-creator")
        skill = _make_skill(db_session, creator, slug="public-vis-skill", is_public=True)
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"public tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(
            name="public-vis-skill", slug="public-vis-skill", version="1.0.0"
        )

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = client.post(
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
            assert resp.status_code == 201, resp.text

            # Search route filters by title/description, not slug. The fixture
            # skill has its title from _make_skill ("Test Skill ..."), so search
            # by that. The semantic we are verifying: is_public=True skills are
            # findable via the public search route.
            search_resp = client.get(
                "/api/skills/search",
                params={"q": "Test Skill"},
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert search_resp.status_code == 200, search_resp.text
        body = search_resp.json()
        slugs = [r["slug"] for r in body.get("results", [])]
        assert "public-vis-skill" in slugs, (
            f"Public skill missing from search results: {slugs}"
        )

        # And the row must persist with is_public=True
        from app.models import Skill as SkillModel
        row = (
            db_session.query(SkillModel)
            .filter(SkillModel.slug == "public-vis-skill")
            .first()
        )
        assert row is not None and row.is_public is True


class TestPublishTomlPersisted:
    """AC8 — skill_versions.skill_toml and checksum_sha256 are persisted correctly."""

    def test_toml_and_sha256_stored_in_db(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="db-check-creator")
        skill = _make_skill(db_session, creator, slug="db-check-skill")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"checksum tarball content"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(name="db-check-skill", slug="db-check-skill", version="2.0.0")

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 201, resp.text
        body = resp.json()

        # Verify the DB row directly
        from uuid import UUID
        skill_id = UUID(body["skill_id"])
        version_row = (
            db_session.query(SkillVersion)
            .filter(SkillVersion.skill_id == skill_id, SkillVersion.semver == "2.0.0")
            .first()
        )
        assert version_row is not None
        assert version_row.skill_toml == toml_bytes.decode()
        expected_sha = hashlib.sha256(tarball_bytes).hexdigest()
        assert version_row.checksum_sha256 == expected_sha


# ─────────────────────────── RCP-PUB-2026-05-18 ──────────────────────────
# Three regression tests for the publish pipeline three-bug fix:
#   1. Re-publish of an existing skill must re-sync description/title/license/tier
#      from the tarball's skill.toml (Issue 1 — description="|" stays forever
#      because the existing-skill code path never touched the row).
#   2. Re-publish of an archived skill must un-archive it (Issue 2 — archived
#      slug stays hidden from catalog → portal build skips it → drift alert).
#   3. /api/skills/{slug} must authenticate via the wr_jwt cookie too, not just
#      x-api-key (Issue 3 — Pro/Pro+ browser users couldn't see paywalled
#      SKILL.md bodies because the API only honored agent api keys).
# ─────────────────────────────────────────────────────────────────────────


class TestPublishExistingSkillResyncsMetadata:
    """RCP-PUB-2026-05-18 §1 — re-publishing an existing skill must overwrite
    stale description/license/tier/title on the parent Skill row from the
    new tarball's frontmatter."""

    def test_republish_overwrites_broken_description(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="resync-creator")
        # Seed an existing skill row with broken description (the prod bug)
        skill = Skill(
            id=uuid4(),
            slug="resync-skill",
            title="Old Title",
            description="|",  # ← the bug: YAML literal indicator leaked into DB
            license="MIT",
            tier="cook",  # ← legacy tier label
            is_public=True,
            creator_id=creator.id,
        )
        db_session.add(skill)
        db_session.commit()
        skill_id = skill.id

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"resync tarball v1"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(
            name="Resync Skill",
            slug="resync-skill",
            version="1.0.1",
            description="A fully-formed description of at least twenty chars.",
            license="Apache-2.0",
            extra={"tier": "pro"},
        )

        client, env = _make_client(db_session, str(tmp_path), is_admin=True)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
                data={"is_public": "true"},
            )
        finally:
            env.stop()

        assert resp.status_code == 201, resp.text
        db_session.expire_all()
        row = db_session.query(Skill).filter(Skill.id == skill_id).first()
        assert row is not None
        assert row.description == "A fully-formed description of at least twenty chars."
        assert row.title == "Resync Skill"
        assert row.license == "Apache-2.0"
        assert row.tier == "pro"


class TestPublishUnarchivesHiddenSkill:
    """RCP-PUB-2026-05-18 §2 — re-publishing an archived skill row must clear
    is_archived so the portal build picks it up."""

    def test_republish_unarchives_skill(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="unarchive-creator")
        skill = _make_skill(db_session, creator, slug="unarchive-skill", is_public=True)
        skill.is_archived = True
        db_session.commit()
        skill_id = skill.id

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"unarchive tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(name="unarchive-skill", slug="unarchive-skill", version="2.0.0")

        client, env = _make_client(db_session, str(tmp_path), is_admin=True)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
                data={"is_public": "true"},
            )
        finally:
            env.stop()

        assert resp.status_code == 201, resp.text
        db_session.expire_all()
        row = db_session.query(Skill).filter(Skill.id == skill_id).first()
        assert row is not None
        assert row.is_archived is False, "publishing a public version must un-archive the skill row"


class TestPublishExistingSkillPreservesNonEmptyDescription:
    """Editorial overrides (e.g. quality_1705 backfill) must survive a re-publish
    when the new tarball happens to ship the SAME description — no unnecessary
    churn — but a NEW description WINS over the row. This test asserts the
    'differs AND non-empty' guard works correctly: same description = no change."""

    def test_no_op_when_description_unchanged(self, db_session, tmp_path):
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="preserve-creator")
        existing_desc = "A fully-formed description of at least twenty chars."
        skill = Skill(
            id=uuid4(),
            slug="preserve-skill",
            title="Preserve Skill",
            description=existing_desc,
            license="MIT",
            tier="pro",
            is_public=True,
            creator_id=creator.id,
        )
        db_session.add(skill)
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"preserve tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        toml_bytes = _valid_toml(
            name="Preserve Skill",
            slug="preserve-skill",
            version="1.0.1",
            description=existing_desc,
            extra={"tier": "pro"},
        )

        client, env = _make_client(db_session, str(tmp_path), is_admin=True)
        try:
            resp = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_bytes), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_bytes), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
                data={"is_public": "true"},
            )
        finally:
            env.stop()

        assert resp.status_code == 201, resp.text
        db_session.expire_all()
        row = db_session.query(Skill).filter(Skill.slug == "preserve-skill").first()
        assert row.description == existing_desc
