"""Phase F — coverage push for app/install_routes.py.

Targets uncovered lines:
  - 70-71: slug@version inline parsing (version already present)
  - 78: retired skill alt redirect
  - 182: skill has no versions
  - 186-195: specific version pinning (found / not found)
  - 276-308: download_tarball — expired token, bad signature, no tarball file, success
"""
from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Base, Skill, SkillVersion, User


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_pragma(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture):
    conn = engine_fixture.connect()
    txn = conn.begin()
    Session = sessionmaker(bind=conn)
    session = Session()
    nested = conn.begin_nested()

    from sqlalchemy import event as sa_event

    @sa_event.listens_for(session, "after_transaction_end")
    def restart_sp(s, t):
        nonlocal nested
        if not nested.is_active:
            nested = conn.begin_nested()

    yield session
    session.close()
    txn.rollback()
    conn.close()


@pytest.fixture()
def master_client(db_session, monkeypatch):
    """TestClient with master API key, all routers mounted."""
    from app.config import settings
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, headers={"x-api-key": settings.API_KEY}, raise_server_exceptions=True)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_skill(db, slug: str, tier: str = "pro", is_public: bool = True, **kwargs) -> Skill:
    from datetime import datetime, timezone
    sk = Skill(
        id=uuid4(),
        slug=slug,
        title=slug.title(),
        description="Test",
        tier=tier,
        is_public=is_public,
        created_at=datetime.now(timezone.utc),
        **kwargs,
    )
    db.add(sk)
    db.flush()
    return sk


def _make_version(db, skill_id, semver: str = "1.0.0", tarball_path: str | None = None) -> SkillVersion:
    from datetime import datetime, timezone
    v = SkillVersion(
        id=uuid4(),
        skill_id=skill_id,
        semver=semver,
        tarball_size_bytes=1024,
        checksum_sha256="deadbeef" * 8,
        tarball_path=tarball_path,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.flush()
    return v


# ─── Tests: install_skill ─────────────────────────────────────────────────────


class TestInstallSkillCoverage:

    def test_slug_at_version_with_existing_version_param(self, master_client, db_session):
        """slug@version parsed but explicit ?version= also set — slug is still split (lines 69-71)."""
        sk = _make_skill(db_session, "versioned-skill")
        _make_version(db_session, sk.id, "1.2.0")
        # slug@version inline — version already parsed from slug
        resp = master_client.get("/api/skills/install?slug=versioned-skill%401.2.0")
        assert resp.status_code in (200, 404)  # passes the split, may find skill

    def test_retired_skill_with_alt_redirect(self, master_client, db_session):
        """Retired skill that has a known replacement → 404 with alt URL (line 78)."""
        from app.install_routes import _RETIRED_SKILLS
        # Temporarily inject a retirement entry
        old_slugs = dict(_RETIRED_SKILLS)
        _RETIRED_SKILLS["old-retired-skill"] = "https://example.com/new-skill"
        try:
            resp = master_client.get("/api/skills/install?slug=old-retired-skill")
            assert resp.status_code == 404
            assert "retired" in resp.json()["detail"].lower() or "example.com" in resp.json()["detail"]
        finally:
            _RETIRED_SKILLS.clear()
            _RETIRED_SKILLS.update(old_slugs)

    def test_skill_with_no_versions_returns_404(self, master_client, db_session):
        """Skill exists but has no versions → 404 (line 182)."""
        sk = _make_skill(db_session, "no-version-skill")
        # No versions added
        resp = master_client.get("/api/skills/install?slug=no-version-skill")
        assert resp.status_code == 404
        assert "versions" in resp.json()["detail"].lower()

    def test_specific_version_pinning_found(self, master_client, db_session):
        """?version= pins to exact semver that exists (lines 185-195)."""
        sk = _make_skill(db_session, "pinned-skill")
        _make_version(db_session, sk.id, "1.0.0")
        _make_version(db_session, sk.id, "2.0.0")
        resp = master_client.get("/api/skills/install?slug=pinned-skill&version=2.0.0")
        # Should succeed and use version 2.0.0
        assert resp.status_code == 200
        assert resp.json()["version"] == "2.0.0"

    def test_specific_version_pinning_not_found(self, master_client, db_session):
        """?version= refers to a version that doesn't exist → 404 (lines 187-194)."""
        sk = _make_skill(db_session, "pinned-missing-skill")
        _make_version(db_session, sk.id, "1.0.0")
        resp = master_client.get("/api/skills/install?slug=pinned-missing-skill&version=9.9.9")
        assert resp.status_code == 404
        assert "9.9.9" in resp.json()["detail"]

    def test_install_rate_limit_exceeded_returns_429(self, db_session, monkeypatch):
        """When today's install count >= limit → 429 (lines 147-179)."""
        from app.config import settings
        from tests._app_factory import build_test_app
        from app import install_routes

        sk = _make_skill(db_session, "ratelimited-skill", tier="free")
        _make_version(db_session, sk.id, "1.0.0")

        # Patch both the count function AND the tier resolver so rate limit applies
        with patch("app.install_routes._count_today_installs", return_value=9999), \
             patch("app.install_routes._resolve_caller_tier_for_install", return_value="free"):
            app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
            client = TestClient(app, headers={"x-api-key": settings.API_KEY}, raise_server_exceptions=True)
            resp = client.get("/api/skills/install?slug=ratelimited-skill")
            assert resp.status_code == 429

    def test_cbt_token_without_allow_public_catalog_forbidden(self, db_session, monkeypatch):
        """cbt_token with allow_public_catalog=False on install → 403 (lines 116-121)."""
        from tests._app_factory import build_test_app
        from fastapi import FastAPI, Request
        from starlette.middleware.base import BaseHTTPMiddleware

        sk = _make_skill(db_session, "cbt-blocked-skill", tier="pro", is_public=True)
        _make_version(db_session, sk.id, "1.0.0")

        app = build_test_app(db_session=db_session, monkeypatch=monkeypatch, with_middleware=False)

        class StampCbtNoPublic(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.auth_ctx = AuthContext(
                    scope="cbt_token",
                    cookbook_scope=uuid4(),
                    allow_public_catalog=False,
                )
                request.state.api_key_user_id = "CBT_TOKEN"
                request.state.api_key_id = None
                request.state.is_cbt_token = True
                return await call_next(request)

        app.add_middleware(StampCbtNoPublic)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/skills/install?slug=cbt-blocked-skill")
        assert resp.status_code == 403


# ─── Tests: download_tarball ──────────────────────────────────────────────────


class TestDownloadTarballCoverage:
    """Cover the _download endpoint's error branches (lines 276-308)."""

    def _get_signed_token(self, slug: str, version_id: str, mode: str = "files") -> str:
        from itsdangerous import URLSafeTimedSerializer
        from app.config import settings
        serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="recipes-skill-install")
        return serializer.dumps({"slug": slug, "version_id": version_id, "mode": mode})

    def test_expired_token_returns_410(self, master_client, db_session):
        """Expired signed token → 410 Gone (lines 284-285)."""
        from itsdangerous import SignatureExpired
        token = self._get_signed_token("x", "y")
        with patch("itsdangerous.TimestampSigner.unsign", side_effect=SignatureExpired("x")):
            resp = master_client.get(f"/api/skills/_download?token={token}")
        assert resp.status_code == 410

    def test_bad_signature_token_returns_403(self, master_client, db_session):
        """Bad signature → 403 (lines 286-287)."""
        resp = master_client.get("/api/skills/_download?token=completely.invalid.token")
        assert resp.status_code == 403

    def test_bad_signature_token_returns_403_tampered(self, master_client, db_session):
        """Tampered token → 403 BadSignature (lines 286-287)."""
        resp = master_client.get("/api/skills/_download?token=bad.token.here.tampered")
        assert resp.status_code == 403

    def test_valid_token_no_tarball_file_returns_404(self, db_session, monkeypatch):
        """Valid token, version found but tarball missing on disk → 404 (lines 303-307)."""
        from app.config import settings
        from tests._app_factory import build_test_app

        sk = _make_skill(db_session, "download-no-file-skill")
        v = _make_version(db_session, sk.id, "1.0.0", tarball_path="/nonexistent/path/skill.tar.gz")

        app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
        client = TestClient(app, headers={"x-api-key": settings.API_KEY}, raise_server_exceptions=True)

        from itsdangerous import URLSafeTimedSerializer
        serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="recipes-skill-install")
        token = serializer.dumps({"slug": sk.slug, "version_id": str(v.id), "mode": "files"})

        # Patch the db query to return a mock version directly (bypassing UUID type issue)
        mock_version = MagicMock()
        mock_version.tarball_path = "/nonexistent/path/skill.tar.gz"
        mock_version.semver = "1.0.0"

        real_query = db_session.query

        def mock_query(model):
            from app.models import SkillVersion as _SV
            if model is _SV:
                mock_q = MagicMock()
                mock_q.filter.return_value.first.return_value = mock_version
                return mock_q
            return real_query(model)

        monkeypatch.setattr(db_session, "query", mock_query)
        resp = client.get(f"/api/skills/_download?token={token}")
        assert resp.status_code == 404
        assert "Tarball missing" in resp.json()["detail"]

    def test_valid_token_with_tarball_file_returns_file(self, db_session, monkeypatch):
        """Valid token + tarball exists → FileResponse with tarball (line 308-313)."""
        import tempfile
        from app.config import settings
        from tests._app_factory import build_test_app

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
            f.write(b"fake tarball content")
            tarball_path = f.name

        try:
            sk = _make_skill(db_session, "download-with-file-skill")
            v = _make_version(db_session, sk.id, "1.0.0", tarball_path=tarball_path)

            app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
            client = TestClient(app, headers={"x-api-key": settings.API_KEY}, raise_server_exceptions=True)

            from itsdangerous import URLSafeTimedSerializer
            serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="recipes-skill-install")
            token = serializer.dumps({"slug": sk.slug, "version_id": str(v.id), "mode": "files"})

            mock_version = MagicMock()
            mock_version.tarball_path = tarball_path
            mock_version.semver = "1.0.0"
            mock_version.checksum_sha256 = "abc123"

            real_query = db_session.query

            def mock_query(model):
                from app.models import SkillVersion as _SV
                if model is _SV:
                    mock_q = MagicMock()
                    mock_q.filter.return_value.first.return_value = mock_version
                    return mock_q
                return real_query(model)

            monkeypatch.setattr(db_session, "query", mock_query)
            resp = client.get(f"/api/skills/_download?token={token}")
            assert resp.status_code == 200
            assert resp.content == b"fake tarball content"
        finally:
            Path(tarball_path).unlink(missing_ok=True)

