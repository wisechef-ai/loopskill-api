"""Tests for Phase D operator-tier fork endpoints (app/forks_routes.py).

Coverage (per Plan v5.4 §D acceptance):
  - Cook tier → 402 on POST /api/forks/create
  - Operator tier → 201 on POST /api/forks/create
  - Version upload preserves sha256 + size on disk and in DB
  - Install with valid signed token streams the bytes back
  - Install token rejected once expired
  - Delete soft-deletes (row stays, visibility=NULL, readme cleared)
"""
from __future__ import annotations

import hashlib
import io
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app.database import get_db
from app.models import Base, Skill, SkillFork, User


# ─────────────────────────── DB Fixtures ────────────────────────────────

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


# ─────────────────────────── Helpers ─────────────────────────────────────

def _make_user(db: Session, *, tier: str | None, status: str | None = "active") -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="Tester",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(user)
    db.flush()
    return user


def _make_skill(db: Session, slug: str = "source-skill", is_public: bool = True) -> Skill:
    s = Skill(
        id=uuid4(),
        slug=slug,
        title="Source Skill",
        description="The skill being forked",
        is_public=is_public,
        license="MIT",
    )
    db.add(s)
    db.flush()
    return s


def _make_app(db: Session, *, api_key_user_id, is_admin: bool = False) -> FastAPI:
    """Build a minimal FastAPI app for testing forks routes — no real APIKey
    middleware, no Redis. Stamps request.state.api_key_user_id directly."""
    from app.forks_routes import router as forks_router

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
            request.state.api_key_user_id = None if is_admin else _uid
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(forks_router)
    return app


# ─────────────────────────── Test cases ──────────────────────────────────


class TestTierGate:
    def test_cook_tier_blocked_with_402(self, db_session, tmp_path):
        user = _make_user(db_session, tier="cook")
        _make_skill(db_session, slug="src-1")
        db_session.commit()

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                r = client.post("/api/forks/create", json={
                    "source_slug": "src-1",
                    "name": "My Cook Fork",
                })
        assert r.status_code == 402, r.text
        assert r.json()["detail"]["needs_tier"] == "operator"

    def test_no_tier_blocked_with_402(self, db_session, tmp_path):
        user = _make_user(db_session, tier=None, status=None)
        _make_skill(db_session, slug="src-2")
        db_session.commit()

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                r = client.post("/api/forks/create", json={
                    "source_slug": "src-2",
                    "name": "Free Tier Fork",
                })
        assert r.status_code == 402

    def test_inactive_subscription_blocked(self, db_session, tmp_path):
        user = _make_user(db_session, tier="operator", status="canceled")
        _make_skill(db_session, slug="src-3")
        db_session.commit()

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                r = client.post("/api/forks/create", json={
                    "source_slug": "src-3",
                    "name": "Cancelled Operator",
                })
        assert r.status_code == 402


class TestCreateFork:
    def test_operator_can_create(self, db_session, tmp_path):
        user = _make_user(db_session, tier="operator")
        skill = _make_skill(db_session, slug="seo-audit-engine")
        db_session.commit()

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                r = client.post("/api/forks/create", json={
                    "source_slug": "seo-audit-engine",
                    "name": "My SEO Fork",
                    "readme": "# my notes",
                })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == "My SEO Fork"
        assert body["slug"] == "my-seo-fork"
        assert body["visibility"] == "private"
        assert body["source_skill_id"] == str(skill.id)
        assert body["source_slug"] == "seo-audit-engine"
        assert body["readme"] == "# my notes"

    def test_studio_can_create(self, db_session, tmp_path):
        user = _make_user(db_session, tier="studio")
        _make_skill(db_session, slug="src-studio")
        db_session.commit()

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                r = client.post("/api/forks/create", json={
                    "source_slug": "src-studio",
                    "name": "Studio Fork",
                })
        assert r.status_code == 201

    def test_unknown_source_returns_404(self, db_session, tmp_path):
        user = _make_user(db_session, tier="operator")
        db_session.commit()

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                r = client.post("/api/forks/create", json={
                    "source_slug": "does-not-exist",
                    "name": "Phantom",
                })
        assert r.status_code == 404


class TestVersionUpload:
    def _create_fork(self, db, user, source_slug):
        skill = _make_skill(db, slug=source_slug)
        fork = SkillFork(
            id=uuid4(),
            user_id=user.id,
            source_skill_id=skill.id,
            name="Version Test Fork",
            slug="version-test-fork",
            visibility="private",
        )
        db.add(fork)
        db.commit()
        return fork

    def test_upload_preserves_sha256_and_size(self, db_session, tmp_path):
        user = _make_user(db_session, tier="operator")
        fork = self._create_fork(db_session, user, "src-vu-1")

        tarball_bytes = b"this-is-a-tarball-payload-" + b"x" * 256
        expected_sha = hashlib.sha256(tarball_bytes).hexdigest()
        expected_size = len(tarball_bytes)

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                r = client.post(
                    f"/api/forks/{fork.id}/version",
                    files={"tarball": ("v.tar.gz", io.BytesIO(tarball_bytes), "application/gzip")},
                    data={"semver": "0.1.0", "changelog": "first cut"},
                )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["semver"] == "0.1.0"
        assert body["checksum_sha256"] == expected_sha
        assert body["tarball_size_bytes"] == expected_size

        # Verify the file actually landed on disk under the configured root
        # with matching sha256 + size.
        from app.models import ForkVersion
        db_session.expire_all()
        v = db_session.query(ForkVersion).filter(ForkVersion.id == UUID(body["id"])).first()
        assert v is not None
        on_disk = Path(v.tarball_path).read_bytes()
        assert hashlib.sha256(on_disk).hexdigest() == expected_sha
        assert len(on_disk) == expected_size
        # latest_version_id pointer updated
        db_session.refresh(fork)
        assert str(fork.latest_version_id) == body["id"]

    def test_invalid_semver_rejected(self, db_session, tmp_path):
        user = _make_user(db_session, tier="operator")
        fork = self._create_fork(db_session, user, "src-vu-2")

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                r = client.post(
                    f"/api/forks/{fork.id}/version",
                    files={"tarball": ("v.tar.gz", io.BytesIO(b"abc"), "application/gzip")},
                    data={"semver": "not-a-version"},
                )
        assert r.status_code == 422

    def test_other_users_fork_404s(self, db_session, tmp_path):
        owner = _make_user(db_session, tier="operator")
        intruder = _make_user(db_session, tier="operator")
        fork = self._create_fork(db_session, owner, "src-vu-3")

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=intruder.id)
            with TestClient(app) as client:
                r = client.post(
                    f"/api/forks/{fork.id}/version",
                    files={"tarball": ("v.tar.gz", io.BytesIO(b"abc"), "application/gzip")},
                    data={"semver": "0.1.0"},
                )
        assert r.status_code == 404


class TestInstallSignedURL:
    def test_install_returns_valid_signed_url_and_download_works(self, db_session, tmp_path):
        user = _make_user(db_session, tier="operator")
        skill = _make_skill(db_session, slug="src-inst")
        fork = SkillFork(
            id=uuid4(),
            user_id=user.id,
            source_skill_id=skill.id,
            name="Install Fork",
            slug="install-fork",
            visibility="private",
        )
        db_session.add(fork)
        db_session.commit()

        tarball_bytes = b"install-payload-bytes"
        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                up = client.post(
                    f"/api/forks/{fork.id}/version",
                    files={"tarball": ("v.tar.gz", io.BytesIO(tarball_bytes), "application/gzip")},
                    data={"semver": "0.1.0"},
                )
                assert up.status_code == 201, up.text

                inst = client.get(f"/api/forks/{fork.id}/install")
                assert inst.status_code == 200, inst.text
                payload = inst.json()
                assert payload["version"] == "0.1.0"
                assert payload["size_bytes"] == len(tarball_bytes)
                tarball_url = payload["tarball_url"]
                assert "token=" in tarball_url
                token = tarball_url.split("token=", 1)[1]

                # _download is mounted on the same app; call it via path only.
                dl = client.get(f"/api/forks/_download?token={token}")
                assert dl.status_code == 200, dl.text
                assert dl.content == tarball_bytes
                assert dl.headers.get("X-Checksum-SHA256") == hashlib.sha256(tarball_bytes).hexdigest()

    def test_expired_install_token_returns_401(self, db_session, tmp_path):
        user = _make_user(db_session, tier="operator")
        skill = _make_skill(db_session, slug="src-exp")
        fork = SkillFork(
            id=uuid4(),
            user_id=user.id,
            source_skill_id=skill.id,
            name="Expired Fork",
            slug="expired-fork",
            visibility="private",
        )
        db_session.add(fork)
        db_session.commit()

        tarball_bytes = b"expired-payload"
        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                client.post(
                    f"/api/forks/{fork.id}/version",
                    files={"tarball": ("v.tar.gz", io.BytesIO(tarball_bytes), "application/gzip")},
                    data={"semver": "0.1.0"},
                )
                # Generate a token with a forced past timestamp by patching
                # itsdangerous's time source to ~10 minutes ago at signing.
                from app import forks_routes
                serializer = forks_routes._make_install_serializer()
                # Sign with an artificially old timestamp (past TTL).
                # itsdangerous embeds a timestamp in the token; we monkey-patch
                # `now` for the signer just for token creation.
                old_token = serializer.dumps({
                    "fork_id": str(fork.id),
                    "version_id": "irrelevant-still-fails-on-sig-timestamp",
                })
                # Sleep just over 1s and use a max_age=0 path: the simpler
                # check is to call the verify with a 0-second window.
                # We exercise _download with a valid token but ttl override:
                with patch.object(
                    forks_routes,
                    "INSTALL_TOKEN_TTL_SECONDS",
                    0,  # any positive elapsed time triggers SignatureExpired
                ):
                    time.sleep(1)
                    dl = client.get(f"/api/forks/_download?token={old_token}")
        assert dl.status_code == 401
        assert dl.json()["detail"] == "token_expired"


class TestSoftDelete:
    def test_delete_clears_visibility_and_readme(self, db_session, tmp_path):
        user = _make_user(db_session, tier="operator")
        skill = _make_skill(db_session, slug="src-del")
        fork = SkillFork(
            id=uuid4(),
            user_id=user.id,
            source_skill_id=skill.id,
            name="Doomed Fork",
            slug="doomed-fork",
            visibility="private",
            readme="# secrets",
        )
        db_session.add(fork)
        db_session.commit()

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                r = client.delete(f"/api/forks/{fork.id}")
                assert r.status_code == 200
                assert r.json()["deleted"] is True

                # Row must remain in DB
                fork_id_uuid = fork.id
                db_session.expire_all()
                still_there = db_session.query(SkillFork).filter(SkillFork.id == fork_id_uuid).first()
                assert still_there is not None
                assert still_there.visibility is None
                assert still_there.readme is None

                # And it must not surface in list_forks
                lst = client.get("/api/forks/list")
                assert lst.status_code == 200
                ids = {f["id"] for f in lst.json()["forks"]}
                assert str(fork.id) not in ids


class TestList:
    def test_list_returns_only_active_forks_for_user(self, db_session, tmp_path):
        user = _make_user(db_session, tier="operator")
        other = _make_user(db_session, tier="operator")
        skill = _make_skill(db_session, slug="src-list")

        mine_active = SkillFork(
            id=uuid4(), user_id=user.id, source_skill_id=skill.id,
            name="A", slug="a", visibility="private",
        )
        mine_deleted = SkillFork(
            id=uuid4(), user_id=user.id, source_skill_id=skill.id,
            name="B", slug="b", visibility="private",
        )
        not_mine = SkillFork(
            id=uuid4(), user_id=other.id, source_skill_id=skill.id,
            name="C", slug="c", visibility="private",
        )
        db_session.add_all([mine_active, mine_deleted, not_mine])
        db_session.commit()
        # Soft-delete via the same pathway production uses (visibility -> NULL)
        mine_deleted.visibility = None
        db_session.commit()

        with patch.dict(os.environ, {"RECIPES_FORKS_DIR": str(tmp_path)}):
            app = _make_app(db_session, api_key_user_id=user.id)
            with TestClient(app) as client:
                r = client.get("/api/forks/list")
        assert r.status_code == 200
        ids = {f["id"] for f in r.json()["forks"]}
        assert str(mine_active.id) in ids
        assert str(mine_deleted.id) not in ids
        assert str(not_mine.id) not in ids
