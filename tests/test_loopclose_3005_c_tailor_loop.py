"""Tests for loopclose_3005 Phase C: close the MCP tailor loop.

The fork flow previously stopped at fork-create. Phase C adds two MCP tools —
recipes_tailor_version (upload a version, base64 transport) and
recipes_cookbook_attach (bridge a fork's latest version into a cookbook as a
real installable Skill) — closing the loop:

    recipes_tailor -> recipes_tailor_version -> recipes_cookbook_attach
                   -> recipes_cookbook_install

Covers:
  Tier gate (Pro+ required; master/free/None rejected) for both tools.
  recipes_tailor_version: happy path, invalid semver, invalid base64, empty,
    fork-not-found, duplicate-semver, latest-pointer advance.
  recipes_cookbook_attach: happy path (promote + mint SkillVersion), no-version
    fork rejected, cookbook ownership (no-oracle 404), slug override, no SKILL.md.
  Full dogfood round-trip: tailor -> version -> attach -> cookbook_install
    returns an installable tarball_url signed with the canonical
    recipes-skill-install salt (salt parity).
  MCP dispatch: both tools registered + dispatched via call_tool_sync.
"""

from __future__ import annotations

import base64
import io
import os
import tarfile
import tempfile
from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.models import Base, Bundle, Skill, SkillFork, User


# ── DB fixtures (self-contained, mirrors test_integrator_w1_tailor.py) ────────


@pytest.fixture(scope="module")
def _engine():
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
def db(_engine) -> Generator[Session, None, None]:
    connection = _engine.connect()
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
def storage_dirs() -> Generator[tuple[str, str], None, None]:
    """Point forks + skills tarball storage at temp dirs for the test."""
    with tempfile.TemporaryDirectory() as forks_dir, tempfile.TemporaryDirectory() as skills_dir:
        old_forks = os.environ.get("RECIPES_FORKS_DIR")
        old_skills = os.environ.get("RECIPES_SKILLS_DIR")
        os.environ["RECIPES_FORKS_DIR"] = forks_dir
        os.environ["RECIPES_SKILLS_DIR"] = skills_dir
        try:
            yield forks_dir, skills_dir
        finally:
            if old_forks is None:
                os.environ.pop("RECIPES_FORKS_DIR", None)
            else:
                os.environ["RECIPES_FORKS_DIR"] = old_forks
            if old_skills is None:
                os.environ.pop("RECIPES_SKILLS_DIR", None)
            else:
                os.environ["RECIPES_SKILLS_DIR"] = old_skills


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_user(db: Session, tier: str) -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="Tester",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(user)
    db.flush()
    return user


def _make_public_skill(db: Session, slug: str) -> Skill:
    s = Skill(id=uuid4(), slug=slug, title="Source Skill", is_public=True)
    db.add(s)
    db.flush()
    return s


def _make_cookbook(db: Session, owner: User) -> Bundle:
    cb = Bundle(id=uuid4(), name="Test Cookbook", bundle_owner=owner.id, is_base=False)
    db.add(cb)
    db.flush()
    return cb


def _make_fork(db: Session, user: User, source: Skill, slug: str = "my-fork") -> SkillFork:
    fork = SkillFork(
        id=uuid4(),
        user_id=user.id,
        source_skill_id=source.id,
        name=slug.replace("-", " ").title(),
        slug=slug,
        readme="initial",
        visibility="private",
    )
    db.add(fork)
    db.flush()
    return fork


def _build_tarball(skill_md: str = "---\nname: tailored\n---\n# Tailored Skill\nbody") -> bytes:
    """Build a minimal .tar.gz containing SKILL.md at the root."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        data = skill_md.encode("utf-8")
        info = tarfile.TarInfo(name="SKILL.md")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _b64_tarball(skill_md: str | None = None) -> str:
    tb = _build_tarball(skill_md) if skill_md is not None else _build_tarball()
    return base64.b64encode(tb).decode("ascii")


def _pro_ctx(user: User, tier: str = "pro") -> AuthContext:
    return AuthContext(scope="user", user_id=user.id, tier=tier)


# ── recipes_tailor_version ─────────────────────────────────────────────────


class TestTailorVersion:
    def test_happy_path(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        user = _make_user(db, "pro")
        source = _make_public_skill(db, "src-tv-1")
        fork = _make_fork(db, user, source, "fork-tv-1")
        db.commit()

        res = recipes_tailor_version(
            db,
            fork_id=str(fork.id),
            tarball_base64=_b64_tarball(),
            semver="1.0.0",
            ctx=_pro_ctx(user),
        )
        assert res["status"] == "versioned"
        assert res["semver"] == "1.0.0"
        assert res["fork_id"] == str(fork.id)
        assert "version_id" in res
        # latest pointer advanced
        db.refresh(fork)
        assert str(fork.latest_version_id) == res["version_id"]

    def test_invalid_semver(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        user = _make_user(db, "pro")
        source = _make_public_skill(db, "src-tv-2")
        fork = _make_fork(db, user, source, "fork-tv-2")
        db.commit()

        res = recipes_tailor_version(
            db, fork_id=str(fork.id), tarball_base64=_b64_tarball(), semver="notsemver", ctx=_pro_ctx(user)
        )
        assert res["code"] == "invalid_semver"

    def test_invalid_base64(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        user = _make_user(db, "pro")
        source = _make_public_skill(db, "src-tv-3")
        fork = _make_fork(db, user, source, "fork-tv-3")
        db.commit()

        res = recipes_tailor_version(
            db, fork_id=str(fork.id), tarball_base64="!!!not base64!!!", semver="1.0.0", ctx=_pro_ctx(user)
        )
        assert res["code"] == "invalid_base64"

    def test_empty_tarball(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        user = _make_user(db, "pro")
        source = _make_public_skill(db, "src-tv-4")
        fork = _make_fork(db, user, source, "fork-tv-4")
        db.commit()

        res = recipes_tailor_version(
            db, fork_id=str(fork.id), tarball_base64=base64.b64encode(b"").decode(), semver="1.0.0", ctx=_pro_ctx(user)
        )
        assert res["code"] == "empty_tarball"

    def test_fork_not_found(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        user = _make_user(db, "pro")
        db.commit()
        res = recipes_tailor_version(
            db, fork_id=str(uuid4()), tarball_base64=_b64_tarball(), semver="1.0.0", ctx=_pro_ctx(user)
        )
        assert res["code"] == "fork_not_found"

    def test_other_users_fork_not_found(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        owner = _make_user(db, "pro")
        attacker = _make_user(db, "pro")
        source = _make_public_skill(db, "src-tv-5")
        fork = _make_fork(db, owner, source, "fork-tv-5")
        db.commit()

        res = recipes_tailor_version(
            db, fork_id=str(fork.id), tarball_base64=_b64_tarball(), semver="1.0.0", ctx=_pro_ctx(attacker)
        )
        assert res["code"] == "fork_not_found"

    def test_duplicate_semver(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        user = _make_user(db, "pro")
        source = _make_public_skill(db, "src-tv-6")
        fork = _make_fork(db, user, source, "fork-tv-6")
        db.commit()

        ctx = _pro_ctx(user)
        r1 = recipes_tailor_version(db, fork_id=str(fork.id), tarball_base64=_b64_tarball(), semver="1.0.0", ctx=ctx)
        assert r1["status"] == "versioned"
        r2 = recipes_tailor_version(db, fork_id=str(fork.id), tarball_base64=_b64_tarball(), semver="1.0.0", ctx=ctx)
        assert r2["code"] == "version_exists"

    def test_free_tier_rejected(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        user = _make_user(db, "free")
        source = _make_public_skill(db, "src-tv-7")
        fork = _make_fork(db, user, source, "fork-tv-7")
        db.commit()

        res = recipes_tailor_version(
            db, fork_id=str(fork.id), tarball_base64=_b64_tarball(), semver="1.0.0", ctx=_pro_ctx(user, "free")
        )
        assert res["code"] == "needs_tier"

    def test_master_key_rejected(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        res = recipes_tailor_version(
            db, fork_id=str(uuid4()), tarball_base64=_b64_tarball(), semver="1.0.0", ctx=AuthContext(scope="master")
        )
        assert res["code"] == "auth_required"


# ── recipes_cookbook_attach ────────────────────────────────────────────────


class TestCookbookAttach:
    def _seed_versioned_fork(self, db, storage_dirs, user, slug, skill_md=None):
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        source = _make_public_skill(db, f"src-{slug}")
        fork = _make_fork(db, user, source, slug)
        db.commit()
        recipes_tailor_version(
            db,
            fork_id=str(fork.id),
            tarball_base64=_b64_tarball(skill_md) if skill_md else _b64_tarball(),
            semver="1.0.0",
            ctx=_pro_ctx(user),
        )
        return fork

    def test_happy_path_promote(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_cookbook_attach
        from app.models import SkillVersion

        user = _make_user(db, "pro")
        fork = self._seed_versioned_fork(db, storage_dirs, user, "fork-att-1")
        cb = _make_cookbook(db, user)
        db.commit()

        res = recipes_cookbook_attach(
            db, fork_id=str(fork.id), target_cookbook_id=str(cb.id), ctx=_pro_ctx(user)
        )
        assert res["status"] in ("created", "updated")
        assert res["cookbook_id"] == str(cb.id)
        assert res["skill_slug"] == "fork-att-1"
        assert res["is_public"] is False
        assert res["version"] == "1.0.0"
        # A real catalog Skill + installable SkillVersion now exist.
        skill = db.query(Skill).filter(Skill.slug == "fork-att-1").first()
        assert skill is not None and skill.is_public is False
        ver = db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).first()
        assert ver is not None and ver.semver == "1.0.0"

    def test_no_version_rejected(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_cookbook_attach

        user = _make_user(db, "pro")
        source = _make_public_skill(db, "src-att-2")
        fork = _make_fork(db, user, source, "fork-att-2")  # no version uploaded
        cb = _make_cookbook(db, user)
        db.commit()

        res = recipes_cookbook_attach(db, fork_id=str(fork.id), target_cookbook_id=str(cb.id), ctx=_pro_ctx(user))
        assert res["code"] == "no_versions"

    def test_cookbook_not_owned(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_cookbook_attach

        owner = _make_user(db, "pro")
        attacker = _make_user(db, "pro")
        fork = self._seed_versioned_fork(db, storage_dirs, attacker, "fork-att-3")
        cb = _make_cookbook(db, owner)  # owned by someone else
        db.commit()

        res = recipes_cookbook_attach(db, fork_id=str(fork.id), target_cookbook_id=str(cb.id), ctx=_pro_ctx(attacker))
        assert res["code"] == "cookbook_not_found"  # no-oracle

    def test_slug_override(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_cookbook_attach

        user = _make_user(db, "pro")
        fork = self._seed_versioned_fork(db, storage_dirs, user, "fork-att-4")
        cb = _make_cookbook(db, user)
        db.commit()

        res = recipes_cookbook_attach(
            db, fork_id=str(fork.id), target_cookbook_id=str(cb.id), slug="custom-deployed-slug", ctx=_pro_ctx(user)
        )
        assert res["skill_slug"] == "custom-deployed-slug"
        assert db.query(Skill).filter(Skill.slug == "custom-deployed-slug").first() is not None

    def test_no_skill_md_in_tarball(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_cookbook_attach, recipes_tailor_version

        user = _make_user(db, "pro")
        source = _make_public_skill(db, "src-att-5")
        fork = _make_fork(db, user, source, "fork-att-5")
        cb = _make_cookbook(db, user)
        db.commit()

        # Upload a tarball with NO SKILL.md
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            data = b"not a skill"
            info = tarfile.TarInfo(name="README.txt")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        b64 = base64.b64encode(buf.getvalue()).decode()
        recipes_tailor_version(db, fork_id=str(fork.id), tarball_base64=b64, semver="1.0.0", ctx=_pro_ctx(user))

        res = recipes_cookbook_attach(db, fork_id=str(fork.id), target_cookbook_id=str(cb.id), ctx=_pro_ctx(user))
        assert res["code"] == "no_skill_md_in_tarball"

    def test_free_tier_rejected(self, db: Session, storage_dirs) -> None:
        from app.mcp.tools.fork_deploy import recipes_cookbook_attach

        user = _make_user(db, "free")
        cb = _make_cookbook(db, user)
        db.commit()
        res = recipes_cookbook_attach(
            db, fork_id=str(uuid4()), target_cookbook_id=str(cb.id), ctx=_pro_ctx(user, "free")
        )
        assert res["code"] == "needs_tier"


# ── Full dogfood round-trip + salt parity ──────────────────────────────────


class TestDogfoodRoundTrip:
    def test_tailor_to_install(self, db: Session, storage_dirs) -> None:
        """tailor -> version -> attach -> cookbook_install, end to end."""
        from app.mcp.tools.fork_deploy import recipes_cookbook_attach, recipes_tailor_version
        from app.mcp.tools.bundle_install import recipes_cookbook_install
        from app.mcp.tools.tailor import recipes_tailor

        user = _make_user(db, "pro")
        source = _make_public_skill(db, "round-trip-src")
        cb = _make_cookbook(db, user)
        db.commit()
        ctx = _pro_ctx(user)

        # 1. tailor (fork)
        t = recipes_tailor(db, source_slug="round-trip-src", name="Round Trip Fork", ctx=ctx)
        assert t["status"] == "forked"
        fork_id = t["fork_id"]

        # 2. version
        v = recipes_tailor_version(db, fork_id=fork_id, tarball_base64=_b64_tarball(), semver="2.1.0", ctx=ctx)
        assert v["status"] == "versioned"

        # 3. attach (promote into cookbook)
        a = recipes_cookbook_attach(db, fork_id=fork_id, target_cookbook_id=str(cb.id), ctx=ctx)
        assert a["status"] in ("created", "updated")
        promoted_slug = a["skill_slug"]

        # 4. cookbook_install — the loop closes: an installable tarball_url
        inst = recipes_cookbook_install(db=db, ctx=ctx, cookbook_id=str(cb.id), slug=promoted_slug)
        assert inst["slug"] == promoted_slug
        assert inst["version"] == "2.1.0"
        assert inst["tarball_url"].startswith("http")
        assert "/api/skills/_download?token=" in inst["tarball_url"]

    def test_salt_parity(self, db: Session, storage_dirs) -> None:
        """The promoted skill's install token verifies under the canonical
        recipes-skill-install salt (same salt install_routes._download uses)."""
        from itsdangerous import URLSafeTimedSerializer
        from urllib.parse import parse_qs, urlparse

        from app.config import settings
        from app.mcp.tools.fork_deploy import recipes_cookbook_attach, recipes_tailor_version
        from app.mcp.tools.bundle_install import recipes_cookbook_install
        from app.mcp.tools.tailor import recipes_tailor

        user = _make_user(db, "pro")
        _make_public_skill(db, "salt-src")
        cb = _make_cookbook(db, user)
        db.commit()
        ctx = _pro_ctx(user)

        fork_id = recipes_tailor(db, source_slug="salt-src", name="Salt Fork", ctx=ctx)["fork_id"]
        recipes_tailor_version(db, fork_id=fork_id, tarball_base64=_b64_tarball(), semver="1.2.3", ctx=ctx)
        a = recipes_cookbook_attach(db, fork_id=fork_id, target_cookbook_id=str(cb.id), ctx=ctx)
        inst = recipes_cookbook_install(db=db, ctx=ctx, cookbook_id=str(cb.id), slug=a["skill_slug"])

        token = parse_qs(urlparse(inst["tarball_url"]).query)["token"][0]
        # Verifies ONLY under the canonical salt — drift would raise BadSignature.
        serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="recipes-skill-install")
        payload = serializer.loads(token, max_age=300)
        assert payload["slug"] == a["skill_slug"]
        assert payload["mode"] == "install"


# ── MCP dispatch integration ───────────────────────────────────────────────


class TestMCPDispatch:
    def test_tools_registered(self) -> None:
        from app.mcp.registry import _tool_definitions

        names = [t.name for t in _tool_definitions()]
        assert "recipes_tailor_version" in names
        assert "recipes_cookbook_attach" in names

    def test_tailor_version_dispatched(self, db: Session, storage_dirs) -> None:
        from app.mcp.server import call_tool_sync

        user = _make_user(db, "pro")
        source = _make_public_skill(db, "disp-src-1")
        fork = _make_fork(db, user, source, "disp-fork-1")
        db.commit()
        ctx = _pro_ctx(user)
        caller = {"scope": "user", "user_id": user.id, "api_key_id": None, "auth_ctx": ctx}

        payload = call_tool_sync(
            "recipes_tailor_version",
            {"fork_id": str(fork.id), "tarball_base64": _b64_tarball(), "semver": "1.0.0"},
            caller=caller,
            db=db,
        )
        assert payload.get("status") == "versioned"

    def test_cookbook_attach_dispatched(self, db: Session, storage_dirs) -> None:
        from app.mcp.server import call_tool_sync
        from app.mcp.tools.fork_deploy import recipes_tailor_version

        user = _make_user(db, "pro")
        source = _make_public_skill(db, "disp-src-2")
        fork = _make_fork(db, user, source, "disp-fork-2")
        cb = _make_cookbook(db, user)
        db.commit()
        ctx = _pro_ctx(user)
        recipes_tailor_version(db, fork_id=str(fork.id), tarball_base64=_b64_tarball(), semver="1.0.0", ctx=ctx)

        caller = {"scope": "user", "user_id": user.id, "api_key_id": None, "auth_ctx": ctx}
        payload = call_tool_sync(
            "recipes_cookbook_attach",
            {"fork_id": str(fork.id), "target_cookbook_id": str(cb.id)},
            caller=caller,
            db=db,
        )
        assert payload.get("status") in ("created", "updated")
