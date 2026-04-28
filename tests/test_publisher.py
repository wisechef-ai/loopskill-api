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


# ─────────────────────────── F-API-02: Path Traversal ───────────────────────

class TestPublishPathTraversal:
    """F-API-02 — path traversal in slug or version is rejected with 422."""

    def test_publish_path_traversal_in_slug_rejected(self, db_session, tmp_path):
        """A slug containing ../ must return 422 and not write any file."""
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="traversal-creator-1")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        # slug via skill.toml name that maps to traversal
        bad_toml = (
            b'[skill]\nname = "../../../etc/pwned"\nslug = "../../../etc/pwned"\n'
            b'version = "1.0.0"\ndescription = "x"\nlicense = "MIT"\nentrypoint = "x"\n'
        )

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
        # No file should have been created under tmp_path
        written = list(tmp_path.rglob("*"))
        assert not written, f"Path traversal wrote files: {written}"

    def test_publish_path_traversal_in_version_rejected(self, db_session, tmp_path):
        """A version like '../evil' must return 422."""
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="traversal-creator-2")
        skill = _make_skill(db_session, creator, slug="traversal-version-skill")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes = b"tarball"
        sig_bytes = _sign_tarball(priv, tarball_bytes)
        bad_toml = (
            b'[skill]\nname = "traversal-version-skill"\nslug = "traversal-version-skill"\n'
            b'version = "../evil"\ndescription = "x"\nlicense = "MIT"\nentrypoint = "x"\n'
        )

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


# ─────────────────────────── F-API-03: Creator auto-create ──────────────────

class TestCreatorRepublish:
    """F-API-03 — creator can republish their own newly-created skill."""

    def test_creator_can_republish_their_own_new_skill(self, db_session, tmp_path):
        """Creator publishes v1.0.0 (auto-creates skill), then v1.0.1 must succeed."""
        user = _make_user(db_session)
        # Do NOT pre-create the skill — let publish auto-create it
        # Pre-create creator so the ownership check passes
        creator = _make_creator(db_session, user, slug="auto-creator-republish")
        db_session.commit()

        priv, pub_bytes = _make_keypair()
        tarball_bytes_v1 = b"v1 tarball"
        sig_v1 = _sign_tarball(priv, tarball_bytes_v1)
        toml_v1 = _valid_toml(name="auto-create-skill", slug="auto-create-skill", version="1.0.0")

        client, env = _make_client(db_session, str(tmp_path), api_key_user_id=user.id)
        try:
            # First publish — auto-creates the skill row + links to creator
            resp1 = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_v1), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes_v1), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_v1), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
            assert resp1.status_code == 201, f"First publish failed: {resp1.text}"

            # Second publish (v1.0.1) — same creator should succeed
            tarball_bytes_v2 = b"v2 tarball"
            sig_v2 = _sign_tarball(priv, tarball_bytes_v2)
            toml_v2 = _valid_toml(name="auto-create-skill", slug="auto-create-skill", version="1.0.1")
            resp2 = client.post(
                "/api/skills/_publish",
                files={
                    "skill_toml": ("skill.toml", io.BytesIO(toml_v2), "text/plain"),
                    "tarball": ("skill.tar.gz", io.BytesIO(tarball_bytes_v2), "application/octet-stream"),
                    "signature": ("sig.bin", io.BytesIO(sig_v2), "application/octet-stream"),
                    "signing_pubkey": ("pub.bin", io.BytesIO(pub_bytes), "application/octet-stream"),
                },
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp2.status_code == 201, f"Republish failed (F-API-03 regression): {resp2.text}"


# ─────────────────────────── F-API-06: skills dir uses WR_ settings ─────────

class TestSkillsDirSettings:
    """F-API-06 — _skills_dir() reads RECIPES_SKILLS_DIR (or WR_ prefix)."""

    def test_skills_dir_uses_settings(self, tmp_path):
        """Setting RECIPES_SKILLS_DIR must be picked up by _skills_dir()."""
        from app.publisher_routes import _skills_dir
        with patch.dict(os.environ, {"RECIPES_SKILLS_DIR": str(tmp_path)}):
            result = _skills_dir()
        assert result == tmp_path


# ─────────────────────────── F-API-08: install event version_semver ─────────

class TestInstallEventVersion:
    """F-API-08 — install event records version_semver."""

    def test_install_event_records_version(self, db_session, tmp_path):
        """After install, the install_event row must have version_semver populated."""
        from app.models import InstallEvent as InstallEventModel

        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="event-creator")
        skill = _make_skill(db_session, creator, slug="event-skill", is_public=True)
        # Add a SkillVersion
        version = SkillVersion(
            id=uuid4(),
            skill_id=skill.id,
            semver="3.0.0",
            tarball_path=str(tmp_path / "fake.tar.gz"),
            tarball_size_bytes=100,
            checksum_sha256="abc123",
        )
        db_session.add(version)
        db_session.commit()

        # Create a fake tarball on disk so the download route doesn't 404
        (tmp_path / "fake.tar.gz").write_bytes(b"x")

        client, env = _make_client(db_session, str(tmp_path), is_admin=True)
        try:
            resp = client.get(
                "/api/skills/install",
                params={"slug": "event-skill"},
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 200, resp.text

        # Query the install event
        event = (
            db_session.query(InstallEventModel)
            .filter(InstallEventModel.skill_slug == "event-skill")
            .first()
        )
        assert event is not None, "Install event not recorded"
        assert event.version_semver == "3.0.0", (
            f"F-API-08: version_semver not populated, got {event.version_semver!r}"
        )


# ─────────────────────────── F-API-07: VERSION = 0.4.0 ──────────────────────

class TestVersionBump:
    """F-API-07 — VERSION in routes.py is 0.4.0."""

    def test_healthz_version_is_0_4_0(self, db_session):
        from app.routes import VERSION
        assert VERSION == "0.4.0", f"F-API-07: VERSION should be 0.4.0, got {VERSION!r}"


# ─────────────────────────── F-API-14: install manifest category ────────────

class TestInstallManifestCategory:
    """F-API-14 — install response includes manifest.category."""

    def test_install_response_includes_manifest_category(self, db_session, tmp_path):
        """Install response must include manifest.category from skill.toml."""
        user = _make_user(db_session)
        creator = _make_creator(db_session, user, slug="manifest-creator")
        skill = _make_skill(db_session, creator, slug="manifest-skill", is_public=True)
        toml_content = (
            '[skill]\nname = "manifest-skill"\nversion = "1.0.0"\n'
            'description = "test"\nlicense = "MIT"\nentrypoint = "run.sh"\n'
            'category = "devops"\n'
        )
        version = SkillVersion(
            id=uuid4(),
            skill_id=skill.id,
            semver="1.0.0",
            tarball_path=str(tmp_path / "manifest.tar.gz"),
            tarball_size_bytes=100,
            checksum_sha256="deadbeef",
            skill_toml=toml_content,
        )
        db_session.add(version)
        db_session.commit()

        (tmp_path / "manifest.tar.gz").write_bytes(b"x")

        client, env = _make_client(db_session, str(tmp_path), is_admin=True)
        try:
            resp = client.get(
                "/api/skills/install",
                params={"slug": "manifest-skill"},
                headers={"x-api-key": "rec_test_key"},
            )
        finally:
            env.stop()

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "manifest" in body, f"F-API-14: 'manifest' missing from response: {body}"
        assert body["manifest"]["category"] == "devops", (
            f"F-API-14: manifest.category should be 'devops', got {body['manifest']}"
        )


# ─────────────────────────── F-API-11: Pydantic mutable default ─────────────

class TestSandboxRunRequestNoSharedState:
    """F-API-11 — SandboxStatusResponse instances do not share mutable defaults."""

    def test_sandbox_run_request_no_shared_state(self):
        """Mutating one SandboxStatusResponse's list must not affect another."""
        from app.sandbox.routes import SandboxStatusResponse

        r1 = SandboxStatusResponse(slug="skill-a", sandbox_supported=False)
        r2 = SandboxStatusResponse(slug="skill-b", sandbox_supported=False)
        r1.validation_warnings.append("a warning")
        assert r2.validation_warnings == [], (
            f"F-API-11: shared mutable default! r2.validation_warnings = {r2.validation_warnings}"
        )


# ─────────────────────────── F-API-09: Stripe transfer return type ──────────

class TestStripeTransferNone:
    """F-API-09 — create_transfer returns None for below-minimum amounts."""

    def test_create_transfer_below_min_returns_none(self):
        """create_transfer with amount_cents < 100 must return None."""
        from app.stripe_service import create_transfer
        result = create_transfer(
            account_id="acct_test",
            amount_cents=50,  # below 100 minimum
            currency="eur",
            description="test",
        )
        assert result is None, f"F-API-09: expected None for below-min transfer, got {result!r}"


# ─────────────────────────── F-API-05: Redis backoff ────────────────────────

class TestRedisBackoff:
    """F-API-05 — Redis unavailable does not thrash connections."""

    def test_redis_unavailable_does_not_thrash(self):
        """When Redis is down, repeated get_redis() calls must only attempt 1 connection."""
        import redis as redis_module
        import app.middleware as mw_module

        # Reset module state
        mw_module._redis_client = None
        mw_module._redis_available = None
        mw_module._redis_next_retry_at = 0.0

        call_count = 0

        def mock_from_url(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise redis_module.ConnectionError("Redis not available")

        with patch("redis.from_url", side_effect=mock_from_url):
            for _ in range(10):
                mw_module.get_redis()

        assert call_count == 1, (
            f"F-API-05: Redis thrashing! Expected 1 connection attempt, got {call_count}"
        )
