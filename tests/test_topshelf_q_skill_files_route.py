"""Phase Q — tests for /api/skills/{slug}/files and /api/skills/{slug}/file.

Covers:
  - Manifest correctness (file list, total_files, total_bytes)
  - Auth gating: free callers → SKILL.md only; pro/master → all files
  - Path-traversal rejection: .., %2e%2e, absolute (/etc/passwd), null byte, symlinks
  - Size cap: file > 1 MiB → 413
  - 404 on missing skill, missing file in tarball
"""

from __future__ import annotations

import io
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Base, Skill, SkillVersion


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def set_pragma(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture):
    conn = engine_fixture.connect()
    txn = conn.begin()
    _Session = sessionmaker(bind=conn)
    session = _Session()
    nested = conn.begin_nested()

    @sa_event.listens_for(session, "after_transaction_end")
    def restart_sp(s, t):
        nonlocal nested
        if not nested.is_active:
            nested = conn.begin_nested()

    yield session
    session.close()
    txn.rollback()
    conn.close()


def _make_skill(db, slug: str = "test-skill", tier: str = "pro", **kwargs) -> Skill:
    sk = Skill(
        id=uuid4(),
        slug=slug,
        title=slug.title(),
        description="Test skill",
        tier=tier,
        category="devops",
        is_public=True,
        created_at=datetime.now(timezone.utc),
        **kwargs,
    )
    db.add(sk)
    db.flush()
    return sk


def _make_version(db, skill_id, semver: str = "1.0.0", tarball_path: str | None = None) -> SkillVersion:
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


def _make_tarball(files: dict[str, bytes], dirname: str = "test-skill-1.0.0") -> str:
    """Create a temp .tar.gz with given {relative_path: content} entries. Returns path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    tmp.close()
    with tarfile.open(tmp.name, "w:gz") as tf:
        # Root directory entry
        info = tarfile.TarInfo(name=dirname)
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
        for rel_path, content in files.items():
            full_path = f"{dirname}/{rel_path}"
            info = tarfile.TarInfo(name=full_path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return tmp.name


def _make_flat_tarball(files: dict[str, bytes]) -> str:
    """Create a temp .tar.gz with entries at the archive ROOT (no wrapping dir).

    Mirrors the flat publish layout used by ~35% of the live catalog, where
    ``SKILL.md`` sits at the tarball root rather than under ``<skill>/``.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    tmp.close()
    with tarfile.open(tmp.name, "w:gz") as tf:
        for rel_path, content in files.items():
            info = tarfile.TarInfo(name=rel_path)
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return tmp.name


def _make_app_with_auth(db_session, auth_ctx: AuthContext):
    """Build a minimal test app that stamps auth_ctx onto request.state."""
    from app.skill_files_routes import router as files_router

    app = FastAPI()

    class StampAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.auth_ctx = auth_ctx
            return await call_next(request)

    app.add_middleware(StampAuth)
    app.include_router(files_router, prefix="/api")

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return app


# ── Manifest tests ─────────────────────────────────────────────────────────────


class TestSkillFilesManifest:

    def test_manifest_lists_files(self, db_session):
        """GET /api/skills/{slug}/files returns the correct manifest."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        tarball_files = {
            "SKILL.md": b"# Test skill",
            "scripts/run.sh": b"#!/bin/bash\necho hello",
            "references/ref.md": b"# References",
        }
        tarball_path = _make_tarball(tarball_files)
        try:
            sk = _make_skill(db_session, slug="manifest-skill")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="master")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=True)

            resp = client.get("/api/skills/manifest-skill/files")
            assert resp.status_code == 200
            data = resp.json()
            assert "version" in data
            assert "files" in data
            assert "total_files" in data
            assert "total_bytes" in data

            paths = {f["path"] for f in data["files"]}
            assert "SKILL.md" in paths
            assert "scripts/run.sh" in paths
            assert "references/ref.md" in paths
            assert data["total_files"] == 3
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_manifest_flat_tarball_lists_files(self, db_session):
        """Flat-packed tarball (no wrapping dir) must still list its files.

        Regression for the 19/55-skills-empty-manifest bug: ``_read_tarball``
        used to strip the top-level path component unconditionally, so a tarball
        with ``SKILL.md`` at the archive root (no ``<skill>/`` wrapper) produced
        an empty manifest. Both layouts exist in the live catalog.
        """
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        # Flat layout — files at archive root, NO wrapping directory.
        tarball_path = _make_flat_tarball({
            "SKILL.md": b"# Flat skill",
            "recipe.yaml": b"name: flat",
            "skill.toml": b"[skill]\nname = 'flat'",
        })
        try:
            sk = _make_skill(db_session, slug="flat-skill")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="master")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=True)

            resp = client.get("/api/skills/flat-skill/files")
            assert resp.status_code == 200
            data = resp.json()
            paths = {f["path"] for f in data["files"]}
            assert paths == {"SKILL.md", "recipe.yaml", "skill.toml"}
            assert data["total_files"] == 3
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_flat_tarball_single_file_content(self, db_session):
        """A flat tarball's individual files are fetchable via /file."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        tarball_path = _make_flat_tarball({"SKILL.md": b"# Flat body here"})
        try:
            sk = _make_skill(db_session, slug="flat-content-skill", tier="free")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="master")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=True)

            resp = client.get("/api/skills/flat-content-skill/file", params={"path": "SKILL.md"})
            assert resp.status_code == 200
            assert resp.json()["content"] == "# Flat body here"
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_manifest_404_unknown_skill(self, db_session):
        """GET /api/skills/nonexistent/files → 404."""
        ctx = AuthContext(scope="master")
        app = _make_app_with_auth(db_session, ctx)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/skills/nonexistent-q-manifest/files")
        assert resp.status_code == 404

    def test_manifest_404_no_tarball(self, db_session):
        """Skill exists but tarball_path is None → 404."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        sk = _make_skill(db_session, slug="no-tarball-skill-q")
        _make_version(db_session, sk.id, tarball_path=None)

        ctx = AuthContext(scope="master")
        app = _make_app_with_auth(db_session, ctx)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/skills/no-tarball-skill-q/files")
        assert resp.status_code == 404

    def test_manifest_total_bytes(self, db_session):
        """total_bytes sums only regular-file sizes."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        content = b"x" * 100
        tarball_files = {"SKILL.md": content}
        tarball_path = _make_tarball(tarball_files)
        try:
            sk = _make_skill(db_session, slug="bytes-skill-q")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="master")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=True)

            resp = client.get("/api/skills/bytes-skill-q/files")
            assert resp.status_code == 200
            assert resp.json()["total_bytes"] == 100
        finally:
            Path(tarball_path).unlink(missing_ok=True)


# ── Auth-gating tests ──────────────────────────────────────────────────────────


class TestSkillFileAuthGating:

    def test_master_can_access_any_file(self, db_session):
        """Master scope can fetch any file including scripts/."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        tarball_files = {
            "SKILL.md": b"# Skill",
            "scripts/run.sh": b"#!/bin/bash",
        }
        tarball_path = _make_tarball(tarball_files, dirname="gated-skill-1.0.0")
        try:
            sk = _make_skill(db_session, slug="gated-skill-q", tier="pro")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="master")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=True)

            resp = client.get("/api/skills/gated-skill-q/file?path=scripts/run.sh")
            assert resp.status_code == 200
            assert resp.json()["path"] == "scripts/run.sh"
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_pro_can_access_any_file(self, db_session):
        """Pro-tier caller can fetch scripts/ files from a pro skill."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        tarball_files = {
            "SKILL.md": b"# Skill",
            "scripts/run.sh": b"#!/bin/bash",
        }
        tarball_path = _make_tarball(tarball_files, dirname="pro-skill-1.0.0")
        try:
            sk = _make_skill(db_session, slug="pro-skill-q", tier="pro")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="user", tier="pro")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=False)

            resp = client.get("/api/skills/pro-skill-q/file?path=scripts/run.sh")
            assert resp.status_code == 200
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_free_user_blocked_from_all_files_of_paid_skill(self, db_session):
        """Free-tier caller is blocked from EVERY file of a pro skill, SKILL.md included.

        paywall_0604: SKILL.md used to be a free "preview" carve-out, which leaked
        the entire curated deliverable. For a paid skill the body IS the product —
        free/anon callers get 403 on SKILL.md and on scripts/ alike. The public
        teaser is the /files manifest (tree) + the metadata card, not the content.
        """
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        tarball_files = {
            "SKILL.md": b"# Skill",
            "scripts/run.sh": b"#!/bin/bash",
        }
        tarball_path = _make_tarball(tarball_files, dirname="free-gated-1.0.0")
        try:
            sk = _make_skill(db_session, slug="free-gated-q", tier="pro")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="user", tier="free")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=False)

            # SKILL.md is now GATED on a paid skill (was the leak)
            resp = client.get("/api/skills/free-gated-q/file?path=SKILL.md")
            assert resp.status_code == 403

            # Non-SKILL.md is also blocked
            resp2 = client.get("/api/skills/free-gated-q/file?path=scripts/run.sh")
            assert resp2.status_code == 403
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_anonymous_blocked_from_non_skillmd(self, db_session):
        """Anonymous caller (no auth_ctx tier) blocked from non-SKILL.md in pro skill."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        tarball_files = {"SKILL.md": b"# Skill", "templates/x.yml": b"template"}
        tarball_path = _make_tarball(tarball_files, dirname="anon-gated-1.0.0")
        try:
            sk = _make_skill(db_session, slug="anon-gated-q", tier="pro")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext.anonymous()
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=False)

            resp = client.get("/api/skills/anon-gated-q/file?path=templates/x.yml")
            assert resp.status_code == 403
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_free_tier_skill_allows_all_files_for_free_user(self, db_session):
        """Free-tier skill: even free callers can access scripts/ (skill is free)."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        tarball_files = {"SKILL.md": b"# Skill", "scripts/run.sh": b"#!/bin/bash"}
        tarball_path = _make_tarball(tarball_files, dirname="free-skill-1.0.0")
        try:
            sk = _make_skill(db_session, slug="free-skill-q", tier="free")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="user", tier="free")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=False)

            resp = client.get("/api/skills/free-skill-q/file?path=scripts/run.sh")
            assert resp.status_code == 200
        finally:
            Path(tarball_path).unlink(missing_ok=True)


# ── Path-traversal tests ───────────────────────────────────────────────────────


class TestSkillFilePathTraversal:
    """MUST all pass — CVE-class security tests."""

    def _make_test_app(self, db_session):
        ctx = AuthContext(scope="master")
        return _make_app_with_auth(db_session, ctx)

    def _make_skill_with_tarball(self, db_session, slug: str):
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        tarball_files = {"SKILL.md": b"safe content"}
        tarball_path = _make_tarball(tarball_files, dirname=f"{slug}-1.0.0")
        sk = _make_skill(db_session, slug=slug)
        _make_version(db_session, sk.id, tarball_path=tarball_path)
        return tarball_path

    def test_dotdot_rejected(self, db_session, tmp_path):
        """.. path component → 400."""
        tarball_path = self._make_skill_with_tarball(db_session, "traversal-dotdot-q")
        try:
            app = self._make_test_app(db_session)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/skills/traversal-dotdot-q/file?path=../etc/passwd")
            assert resp.status_code == 400
            assert "traversal" in resp.json()["detail"].lower() or "invalid" in resp.json()["detail"].lower()
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_encoded_dotdot_rejected(self, db_session):
        """%2e%2e path → 400 (decoded to .. by FastAPI before our check)."""
        tarball_path = self._make_skill_with_tarball(db_session, "traversal-enc-q")
        try:
            app = self._make_test_app(db_session)
            client = TestClient(app, raise_server_exceptions=False)
            # TestClient sends the URL as-is; ASGI/starlette decodes %2e → .
            resp = client.get("/api/skills/traversal-enc-q/file?path=%2e%2e%2fetc%2fpasswd")
            assert resp.status_code == 400
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_absolute_path_rejected(self, db_session):
        """/etc/passwd → 400 — absolute paths not allowed."""
        tarball_path = self._make_skill_with_tarball(db_session, "traversal-abs-q")
        try:
            app = self._make_test_app(db_session)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/skills/traversal-abs-q/file?path=/etc/passwd")
            assert resp.status_code == 400
            assert "absolute" in resp.json()["detail"].lower() or "invalid" in resp.json()["detail"].lower()
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_null_byte_rejected(self, db_session):
        """Null byte in path → 400."""
        tarball_path = self._make_skill_with_tarball(db_session, "traversal-null-q")
        try:
            app = self._make_test_app(db_session)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/skills/traversal-null-q/file?path=SKILL.md%00evil")
            assert resp.status_code == 400
            assert "null" in resp.json()["detail"].lower() or "invalid" in resp.json()["detail"].lower()
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_path_too_long_rejected(self, db_session):
        """Path > 256 chars → 400."""
        tarball_path = self._make_skill_with_tarball(db_session, "traversal-long-q")
        try:
            app = self._make_test_app(db_session)
            client = TestClient(app, raise_server_exceptions=False)
            long_path = "a" * 300
            resp = client.get(f"/api/skills/traversal-long-q/file?path={long_path}")
            assert resp.status_code == 400
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_dotdot_in_middle_rejected(self, db_session):
        """scripts/../../../etc/passwd → 400."""
        tarball_path = self._make_skill_with_tarball(db_session, "traversal-mid-q")
        try:
            app = self._make_test_app(db_session)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/skills/traversal-mid-q/file?path=scripts/../../../etc/passwd")
            assert resp.status_code == 400
        finally:
            Path(tarball_path).unlink(missing_ok=True)


# ── Size-cap tests ─────────────────────────────────────────────────────────────


class TestSkillFileSizeCap:

    def test_file_over_1mb_returns_413(self, db_session):
        """File > 1 MiB → 413 Request Entity Too Large."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        big_content = b"x" * (1 * 1024 * 1024 + 1)  # 1 MiB + 1 byte
        tarball_files = {"SKILL.md": big_content}
        tarball_path = _make_tarball(tarball_files, dirname="big-skill-1.0.0")
        try:
            sk = _make_skill(db_session, slug="big-skill-q", tier="pro")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="master")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=False)

            resp = client.get("/api/skills/big-skill-q/file?path=SKILL.md")
            assert resp.status_code == 413
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_file_exactly_1mb_is_allowed(self, db_session):
        """File exactly 1 MiB → 200 (boundary condition)."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        exact_content = b"x" * (1 * 1024 * 1024)  # exactly 1 MiB
        tarball_files = {"SKILL.md": exact_content}
        tarball_path = _make_tarball(tarball_files, dirname="exact-skill-1.0.0")
        try:
            sk = _make_skill(db_session, slug="exact-skill-q", tier="free")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="master")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=True)

            resp = client.get("/api/skills/exact-skill-q/file?path=SKILL.md")
            assert resp.status_code == 200
        finally:
            Path(tarball_path).unlink(missing_ok=True)

    def test_missing_file_in_tarball_returns_404(self, db_session):
        """Requesting a file that doesn't exist in the tarball → 404."""
        from app import skill_file_cache
        skill_file_cache.clear_cache()

        tarball_files = {"SKILL.md": b"# content"}
        tarball_path = _make_tarball(tarball_files, dirname="missing-file-1.0.0")
        try:
            sk = _make_skill(db_session, slug="missing-file-q", tier="free")
            _make_version(db_session, sk.id, tarball_path=tarball_path)

            ctx = AuthContext(scope="master")
            app = _make_app_with_auth(db_session, ctx)
            client = TestClient(app, raise_server_exceptions=False)

            resp = client.get("/api/skills/missing-file-q/file?path=does_not_exist.sh")
            assert resp.status_code == 404
        finally:
            Path(tarball_path).unlink(missing_ok=True)
