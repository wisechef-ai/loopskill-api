"""paywall_0604 — regression: SKILL.md of a PAID catalog skill must NOT leak to
anonymous/free callers.

THE BUG (shipped in topshelf_2605 #347, Phase Q file browser):
``get_skill_file`` carved SKILL.md out of the tier paywall — for ANY skill,
free/anon callers could fetch ``?path=SKILL.md`` and receive the full body.
For a curated **paid** skill the SKILL.md *is* the product (instructions,
sub-commands, config schema), so this gave away the entire Pro deliverable for
free. Two prior tests (``test_topshelf_q_skill_files_route.py::
test_free_user_can_only_access_skill_md`` and
``test_w0_1_skill_files_public_middleware.py::test_pro_skillmd_still_public_no_key``)
enshrined the leak as intended behaviour — they are corrected alongside this fix.

THE CONTRACT (Adam, 2026-06-04 — "curated catalog stays Pro; federation is open"):
  - PAID skill (tier != free): SKILL.md + every other file require a paid caller.
    Anon/free → 403. The file *manifest* (/files) stays public (teaser: tree only).
  - FREE skill (tier == free): all files incl. SKILL.md remain public (200).
  - Federation/external skills are served by a DIFFERENT route
    (/api/skills/external/...) and are unaffected — they stay open by design.

These tests are RED before the fix and GREEN after.
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


# ── Fixtures ───────────────────────────────────────────────────────────────


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
def db_session(engine_fixture):
    conn = engine_fixture.connect()
    txn = conn.begin()
    _Session = sessionmaker(bind=conn)
    session = _Session()
    nested = conn.begin_nested()

    @sa_event.listens_for(session, "after_transaction_end")
    def _restart_sp(s, t):
        nonlocal nested
        if not nested.is_active:
            nested = conn.begin_nested()

    yield session
    session.close()
    txn.rollback()
    conn.close()


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


def _seed(db, slug: str, tier: str) -> str:
    tar_path = _make_tarball(
        {"SKILL.md": b"# SECRET PAID BODY\nThe curated instructions live here.\n"},
        dirname=f"{slug}-1.0.0",
    )
    sk = Skill(
        id=uuid4(),
        slug=slug,
        title=slug.title(),
        description="paywall_0604 regression skill",
        tier=tier,
        category="automation",
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
        checksum_sha256="feedface" * 8,
        tarball_path=tar_path,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.flush()
    return tar_path


def _app(db_session, auth_ctx: AuthContext) -> FastAPI:
    from app.skill_files_routes import router as files_router

    app = FastAPI()

    class StampAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.auth_ctx = auth_ctx
            return await call_next(request)

    app.add_middleware(StampAuth)
    app.include_router(files_router, prefix="/api")

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return app


# ── The leak regression — these are the load-bearing assertions ──────────────


class TestPaidSkillMdIsGated:
    def test_anon_cannot_read_skillmd_of_paid_skill(self, db_session):
        """Anonymous caller fetching SKILL.md of a PRO skill → 403 (was 200 leak)."""
        from app import skill_file_cache

        skill_file_cache.clear_cache()
        tar = _seed(db_session, "paywall-pro-anon", tier="pro")
        try:
            client = TestClient(
                _app(db_session, AuthContext.anonymous()),
                raise_server_exceptions=False,
            )
            resp = client.get("/api/skills/paywall-pro-anon/file", params={"path": "SKILL.md"})
            assert resp.status_code == 403, (
                f"PAYWALL LEAK: anon got SKILL.md of a pro skill. Body: {resp.text[:200]}"
            )
            assert b"SECRET PAID BODY" not in resp.content
        finally:
            Path(tar).unlink(missing_ok=True)

    def test_free_user_cannot_read_skillmd_of_paid_skill(self, db_session):
        """Free-tier caller fetching SKILL.md of a PRO skill → 403."""
        from app import skill_file_cache

        skill_file_cache.clear_cache()
        tar = _seed(db_session, "paywall-pro-free", tier="pro")
        try:
            client = TestClient(
                _app(db_session, AuthContext(scope="user", tier="free")),
                raise_server_exceptions=False,
            )
            resp = client.get("/api/skills/paywall-pro-free/file", params={"path": "SKILL.md"})
            assert resp.status_code == 403
            assert b"SECRET PAID BODY" not in resp.content
        finally:
            Path(tar).unlink(missing_ok=True)

    def test_pro_user_can_read_skillmd_of_paid_skill(self, db_session):
        """Paid caller CAN read SKILL.md of a pro skill → 200 (no regression for buyers)."""
        from app import skill_file_cache

        skill_file_cache.clear_cache()
        tar = _seed(db_session, "paywall-pro-paid", tier="pro")
        try:
            client = TestClient(
                _app(db_session, AuthContext(scope="user", tier="pro")),
                raise_server_exceptions=False,
            )
            resp = client.get("/api/skills/paywall-pro-paid/file", params={"path": "SKILL.md"})
            assert resp.status_code == 200
            assert b"SECRET PAID BODY" in resp.content
        finally:
            Path(tar).unlink(missing_ok=True)

    def test_master_can_read_skillmd_of_paid_skill(self, db_session):
        """Master scope is never paywalled."""
        from app import skill_file_cache

        skill_file_cache.clear_cache()
        tar = _seed(db_session, "paywall-pro-master", tier="pro")
        try:
            client = TestClient(
                _app(db_session, AuthContext(scope="master")),
                raise_server_exceptions=False,
            )
            resp = client.get("/api/skills/paywall-pro-master/file", params={"path": "SKILL.md"})
            assert resp.status_code == 200
            assert b"SECRET PAID BODY" in resp.content
        finally:
            Path(tar).unlink(missing_ok=True)


class TestFreeSkillStaysOpen:
    def test_anon_can_read_skillmd_of_free_skill(self, db_session):
        """FREE skill: anon SKILL.md stays public (200) — federation/free UX intact."""
        from app import skill_file_cache

        skill_file_cache.clear_cache()
        tar = _seed(db_session, "paywall-free-anon", tier="free")
        try:
            client = TestClient(
                _app(db_session, AuthContext.anonymous()),
                raise_server_exceptions=False,
            )
            resp = client.get("/api/skills/paywall-free-anon/file", params={"path": "SKILL.md"})
            assert resp.status_code == 200
            assert b"SECRET PAID BODY" in resp.content  # free skill — body is meant to be open
        finally:
            Path(tar).unlink(missing_ok=True)

    def test_manifest_stays_public_for_paid_skill(self, db_session):
        """The file TREE (/files manifest) stays a public teaser even for a paid skill."""
        from app import skill_file_cache

        skill_file_cache.clear_cache()
        tar = _seed(db_session, "paywall-pro-manifest", tier="pro")
        try:
            client = TestClient(
                _app(db_session, AuthContext.anonymous()),
                raise_server_exceptions=False,
            )
            resp = client.get("/api/skills/paywall-pro-manifest/files")
            assert resp.status_code == 200
            paths = {f["path"] for f in resp.json()["files"]}
            assert "SKILL.md" in paths  # listing the name is fine; content is gated
        finally:
            Path(tar).unlink(missing_ok=True)
