"""W0.1 (integrator_2905) — regression: skill-file routes are PUBLIC at the
real APIKeyMiddleware seam.

The Phase-Q file-browser routes (`GET /api/skills/{slug}/files` and
`GET /api/skills/{slug}/file`) shipped in topshelf_2605 but were never added to
the middleware's public allow-list, so `APIKeyMiddleware` returned a bare 401
*before* the route ran. The existing Phase-Q suite
(`test_topshelf_q_skill_files_route.py`) never caught this because it mounts the
router behind a `StampAuth` stub that bypasses `APIKeyMiddleware` entirely — so
the middleware↔route integration seam was untested.

This file pins the seam with the PRODUCTION middleware via `build_test_app`:

  - `/files` with NO api-key  → 200 (public manifest)
  - `/file?path=SKILL.md` (free skill, no key) → 200 (public content)
  - `/file?path=SKILL.md` (PAID skill, no key) → 403 (paywall_0604 — the body is
    the curated deliverable; gated like any other file, never a bare 401)
  - `/file?path=secret.py` (pro skill, no key) → 403, never bare-401
    (the route's own tier paywall fires, proving the request reached the route)
  - a source-grep guard so a future refactor that strips the `{"files", "file"}`
    suffix branch from the middleware trips CI.

Same bug class as 2026-05-19 P1 on `/api/skills/access`.
"""
from __future__ import annotations

import io
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Skill, SkillVersion


# ── DB fixtures (module engine, per-test rollback) ──────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def _pragma(conn, _rec):
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


@pytest.fixture()
def app_with_real_middleware(db, monkeypatch) -> FastAPI:
    """Production-wired app: real APIKeyMiddleware + every router create_app mounts."""
    from tests._app_factory import build_test_app

    return build_test_app(db_session=db, monkeypatch=monkeypatch)


# ── Seed helpers ────────────────────────────────────────────────────────────


def _make_tarball(files: dict[str, bytes], dirname: str) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    tmp.close()
    with tarfile.open(tmp.name, "w:gz") as tf:
        info = tarfile.TarInfo(name=dirname)
        info.type = tarfile.DIRTYPE
        tf.addfile(info)
        for rel_path, content in files.items():
            ti = tarfile.TarInfo(name=f"{dirname}/{rel_path}")
            ti.size = len(content)
            tf.addfile(ti, io.BytesIO(content))
    return tmp.name


def _seed_skill_with_tarball(db, slug: str, tier: str, files: dict[str, bytes]) -> Skill:
    dirname = f"{slug}-1.0.0"
    tar_path = _make_tarball(files, dirname=dirname)
    sk = Skill(
        id=uuid4(),
        slug=slug,
        title=slug.title(),
        description="W0.1 regression skill",
        tier=tier,
        category="devops",
        is_public=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    v = SkillVersion(
        id=uuid4(),
        skill_id=sk.id,
        semver="1.0.0",
        tarball_size_bytes=2048,
        checksum_sha256="cafebabe" * 8,
        tarball_path=tar_path,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.flush()
    return sk


# ── Tests: the real middleware seam ─────────────────────────────────────────


class TestSkillFilesPublicAtMiddleware:
    def test_files_manifest_public_no_key(self, app_with_real_middleware, db):
        """GET /api/skills/{slug}/files with NO api-key → 200 (was bare-401)."""
        _seed_skill_with_tarball(
            db, "w01-free", "free", {"SKILL.md": b"# Hello\n", "run.py": b"print(1)\n"}
        )
        client = TestClient(app_with_real_middleware)
        resp = client.get("/api/skills/w01-free/files")  # deliberately no x-api-key
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["version"] == "1.0.0"
        assert body["total_files"] >= 1
        paths = {f["path"] for f in body["files"]}
        assert "SKILL.md" in paths

    def test_file_skillmd_public_no_key_on_free_skill(self, app_with_real_middleware, db):
        """GET /api/skills/{slug}/file?path=SKILL.md (free skill, no key) → 200."""
        _seed_skill_with_tarball(
            db, "w01-free2", "free", {"SKILL.md": b"# Free skill body\n"}
        )
        client = TestClient(app_with_real_middleware)
        resp = client.get("/api/skills/w01-free2/file", params={"path": "SKILL.md"})
        assert resp.status_code == 200, resp.text
        assert b"Free skill body" in resp.content

    def test_pro_gated_file_no_key_is_403_never_bare_401(self, app_with_real_middleware, db):
        """A pro-tier non-SKILL.md file with NO key returns the route's own 403
        tier-paywall — proving the request REACHED the route (not a middleware
        401). This is the load-bearing assertion: the seam is open, the paywall
        still closes.
        """
        _seed_skill_with_tarball(
            db, "w01-pro", "pro", {"SKILL.md": b"# Pro readme\n", "secret.py": b"SECRET=1\n"}
        )
        client = TestClient(app_with_real_middleware)
        resp = client.get("/api/skills/w01-pro/file", params={"path": "secret.py"})
        # Must NOT be the middleware's bare 401 "Invalid or missing x-api-key header"
        assert resp.status_code != 401, (
            "Middleware bare-401'd before the route ran — W0.1 regression. "
            f"Body: {resp.text}"
        )
        assert resp.status_code == 403, resp.text
        assert "Pro subscription required" in resp.json()["detail"]

    def test_pro_skillmd_gated_no_key_is_403_never_bare_401(self, app_with_real_middleware, db):
        """paywall_0604: on a PAID skill, SKILL.md with NO key is the route's own
        403 tier-paywall — NOT a public 200 (the old leak) and NOT a middleware
        bare-401. The middleware seam stays open (request reaches the route); the
        route's paywall closes on the body. SKILL.md is the curated deliverable,
        so it is gated exactly like scripts/."""
        _seed_skill_with_tarball(
            db, "w01-pro2", "pro", {"SKILL.md": b"# Pro readme\n", "x.py": b"1\n"}
        )
        client = TestClient(app_with_real_middleware)
        resp = client.get("/api/skills/w01-pro2/file", params={"path": "SKILL.md"})
        assert resp.status_code != 401, (
            "Middleware bare-401'd before the route ran — W0.1 regression. "
            f"Body: {resp.text}"
        )
        assert resp.status_code == 403, resp.text
        assert "Pro subscription required" in resp.json()["detail"]
        assert b"Pro readme" not in resp.content


class TestMiddlewareSourceGuard:
    """Grep guard — a refactor that strips the public {files,file} branch from
    the middleware MUST trip CI here, not silently re-break the 401."""

    def test_middleware_allowlists_file_suffixes(self):
        src = Path("app/middleware/api_key.py").read_text(encoding="utf-8")
        assert '{"files", "file"}' in src, (
            "APIKeyMiddleware no longer allow-lists the /skills/{slug}/files and "
            "/file suffixes as public — this re-introduces the W0.1 bare-401 bug. "
            "Restore the suffix branch in dispatch()."
        )
