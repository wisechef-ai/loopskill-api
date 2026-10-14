"""loopskill-api#405 — one pin model for bundle_status, loopskill_sync, lock/drift and install.

Before this fix the server contradicted itself on every MCP call:

* ``bundle_status`` counted every ``pinned_version IS NULL`` row as outdated and
  told the agent to "Call loopskill_sync(...) to apply updates". An unpinned row
  is never stale — ``bundle_install._resolve_version`` serves the latest — and
  ``loopskill_sync`` PINS it, so following the hint froze the bundle (prod:
  224/224 rows unpinned, so every agent got the hint).
* ``loopskill_sync`` wrote ``pinned_version`` but left ``pin_mode='track'``; the
  lock/drift resolver decides on ``pin_mode`` and kept following the head while
  install honoured the pin.
* no MCP tool could unpin.

The acceptance test walks the whole lifecycle through the real MCP tool
functions on sqlite.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.mcp.bundle_status import decorate_with_bundle_status, get_bundle_status
from app.mcp.tools.bundle_install import loopskill_bundle_install
from app.mcp.tools.bundle_stream import loopskill_compose_bundle_from_links
from app.mcp.tools.loopskill_sync import loopskill_sync
from app.models import Base, Bundle, BundleSkill, Skill, SkillVersion, User
from app.services.drift_service import resolve_bundle_entries

_T0 = datetime(2026, 1, 1, 12, 0, 0)


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
    session = sessionmaker(bind=connection, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _user(db) -> User:
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


def _ctx(user: User) -> AuthContext:
    return AuthContext(scope="user", user_id=user.id, tier="pro")


def _skill(db, slug: str) -> Skill:
    s = Skill(id=uuid.uuid4(), slug=slug, title=slug, is_public=True, tier="free")
    db.add(s)
    db.commit()
    return s


def _publish(db, skill: Skill, semver: str, minutes: int) -> SkillVersion:
    """A new version, ``minutes`` after T0 — install orders unpinned rows by created_at."""
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=skill.id,
        semver=semver,
        checksum_sha256="a" * 64,
        created_at=_T0 + timedelta(minutes=minutes),
    )
    db.add(v)
    db.commit()
    return v


def _bundle(db, owner: User, *members: tuple[Skill, str | None]) -> Bundle:
    cb = Bundle(id=uuid.uuid4(), name="B405", bundle_owner=owner.id)
    db.add(cb)
    db.flush()
    for skill, pin in members:
        db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added", pinned_version=pin))
    db.commit()
    return cb


def _row(db, cb_id, skill: Skill) -> BundleSkill:
    db.expire_all()
    return db.query(BundleSkill).filter_by(bundle_id=cb_id, skill_id=skill.id).one()


def _installed(db, ctx, cb_id, slug: str) -> str:
    return loopskill_bundle_install(db=db, ctx=ctx, cookbook_id=str(cb_id), slug=slug)["version"]


def _locked(db, cb_id, slug: str) -> str:
    db.expire_all()
    return {e["slug"]: e["version"] for e in resolve_bundle_entries(db, cb_id)}[slug]


# ── 1. bundle_status: unpinned is never outdated ────────────────────────────


def test_unpinned_skill_behind_latest_is_not_outdated(db):
    owner = _user(db)
    s = _skill(db, "i405-unpinned")
    _publish(db, s, "1.0.0", 0)
    _publish(db, s, "1.1.0", 1)
    _bundle(db, owner, (s, None))

    assert get_bundle_status(db, owner.id) is None, "an unpinned skill always installs latest — not outdated"


def test_unpinned_bundle_gets_no_sync_hint_in_mcp_decoration(db):
    owner = _user(db)
    s = _skill(db, "i405-deco")
    _publish(db, s, "1.0.0", 0)
    _publish(db, s, "2.0.0", 1)
    _bundle(db, owner, (s, None))

    out = decorate_with_bundle_status(db, {"ok": True}, {"user_id": owner.id}, "loopskill_search")
    assert "bundle_status" not in out


def test_pinned_behind_is_outdated_and_hint_says_sync_advances_pins(db):
    owner = _user(db)
    pinned = _skill(db, "i405-pinned")
    _publish(db, pinned, "1.0.0", 0)
    _publish(db, pinned, "1.1.0", 1)
    free = _skill(db, "i405-tracking")
    _publish(db, free, "1.0.0", 0)
    _publish(db, free, "1.1.0", 1)
    cb = _bundle(db, owner, (pinned, "1.0.0"), (free, None))

    status = get_bundle_status(db, owner.id)
    assert status is not None
    (entry,) = status["your_cookbooks"]
    assert entry["id"] == str(cb.id)
    assert entry["updates_available"] == 1
    assert [s["slug"] for s in entry["outdated_skills"]] == ["i405-pinned"]
    assert entry["outdated_skills"][0]["current"] == "1.0.0"
    assert entry["outdated_skills"][0]["latest"] == "1.1.0"

    hint = status["hint"]
    assert "apply updates" not in hint.lower()
    assert "loopskill_sync" in hint and "pin" in hint.lower()
    assert "unpin" in hint
    assert "loopskill_bundle_install" in hint


# ── 2. registry tells the truth about sync ──────────────────────────────────


def _sync_tool():
    from app.mcp.registry import _tool_definitions

    return next(t for t in _tool_definitions() if t.name == "loopskill_sync")


def test_registry_describes_sync_as_pinning_not_updating():
    desc = _sync_tool().description.lower()
    assert "applies updates" not in desc and "apply updates" not in desc
    assert "pins" in desc
    assert "loopskill_bundle_install" in desc
    assert "unpin" in desc


def test_registry_schema_declares_unpin_boolean():
    props = _sync_tool().inputSchema["properties"]
    assert props["unpin"]["type"] == "boolean"
    assert props["unpin"]["default"] is False


# ── 3. sync sets pin_mode with pinned_version ───────────────────────────────


def test_sync_sets_pin_mode_pin_with_pinned_version(db):
    owner = _user(db)
    s = _skill(db, "i405-pinmode")
    _publish(db, s, "1.0.0", 0)
    _publish(db, s, "1.2.0", 1)
    cb = _bundle(db, owner, (s, None))

    out = loopskill_sync(db, cookbook_id=str(cb.id), ctx=_ctx(owner))
    assert out["applied"] is True
    row = _row(db, cb.id, s)
    assert row.pinned_version == "1.2.0"
    assert row.pin_mode == "pin", "a sync pin the lock resolver reads as 'track' is a half pin"


# ── 4. unpin ────────────────────────────────────────────────────────────────


def test_unpin_dry_run_reports_and_writes_nothing(db):
    owner = _user(db)
    s = _skill(db, "i405-unpin-dry")
    _publish(db, s, "1.0.0", 0)
    _publish(db, s, "1.1.0", 1)
    cb = _bundle(db, owner, (s, "1.0.0"))
    row = _row(db, cb.id, s)
    row.pin_mode = "pin"
    db.commit()

    out = loopskill_sync(db, cookbook_id=str(cb.id), unpin=True, dry_run=True, ctx=_ctx(owner))
    assert out["changes"] == [{"slug": "i405-unpin-dry", "from": "1.0.0", "to": None, "action": "unpin"}]
    assert out.get("applied") is not True
    row = _row(db, cb.id, s)
    assert (row.pinned_version, row.pin_mode) == ("1.0.0", "pin")


def test_unpin_clears_pin_and_resets_track(db):
    owner = _user(db)
    s = _skill(db, "i405-unpin")
    _publish(db, s, "1.0.0", 0)
    _publish(db, s, "1.1.0", 1)
    cb = _bundle(db, owner, (s, "1.0.0"))

    out = loopskill_sync(db, cookbook_id=str(cb.id), unpin=True, ctx=_ctx(owner))
    assert out["applied"] is True
    row = _row(db, cb.id, s)
    assert (row.pinned_version, row.pin_mode) == (None, "track")

    again = loopskill_sync(db, cookbook_id=str(cb.id), unpin=True, ctx=_ctx(owner))
    assert again["changes"] == []


def test_unpin_same_owner_authz_as_sync(db):
    owner = _user(db)
    stranger = _user(db)
    s = _skill(db, "i405-unpin-authz")
    _publish(db, s, "1.0.0", 0)
    cb = _bundle(db, owner, (s, "1.0.0"))

    out = loopskill_sync(db, cookbook_id=str(cb.id), unpin=True, ctx=_ctx(stranger))
    assert out["error"] == "not_found"
    assert _row(db, cb.id, s).pinned_version == "1.0.0"


def test_mcp_dispatch_passes_unpin(db):
    from app.mcp.server import call_tool_sync

    owner = _user(db)
    s = _skill(db, "i405-dispatch")
    _publish(db, s, "1.0.0", 0)
    cb = _bundle(db, owner, (s, "1.0.0"))

    out = call_tool_sync(
        "loopskill_sync",
        {"bundle_id": str(cb.id), "unpin": True},
        caller={"scope": "operator", "user_id": owner.id},  # legacy alias (test caller shape)
        db=db,
    )
    assert "error" not in out, out
    assert _row(db, cb.id, s).pinned_version is None


# ── 5. acceptance (issue #405) ──────────────────────────────────────────────


def test_acceptance_compose_track_sync_pin_unpin(db):
    owner = _user(db)
    ctx = _ctx(owner)
    s = _skill(db, "i405-acceptance")
    _publish(db, s, "1.0.0", 0)

    composed = loopskill_compose_bundle_from_links(db, links=["skill://i405-acceptance"], ctx=ctx)
    cb_id = uuid.UUID(composed["cookbook"])
    assert _row(db, cb_id, s).pinned_version is None  # compose leaves it unpinned

    # (a) + (b): a new version on an unpinned bundle is not "outdated" and re-install gets it.
    _publish(db, s, "1.1.0", 1)
    assert get_bundle_status(db, owner.id) is None
    assert _installed(db, ctx, cb_id, "i405-acceptance") == "1.1.0"
    assert _locked(db, cb_id, "i405-acceptance") == "1.1.0"

    # (c): sync pins; the next publish does not move install or the lock.
    synced = loopskill_sync(db, cookbook_id=str(cb_id), ctx=ctx)
    assert synced["applied"] is True
    _publish(db, s, "1.2.0", 2)
    assert _installed(db, ctx, cb_id, "i405-acceptance") == "1.1.0"
    assert _locked(db, cb_id, "i405-acceptance") == "1.1.0", "lock/drift must agree with install"
    status = get_bundle_status(db, owner.id)
    assert status is not None
    assert status["your_cookbooks"][0]["outdated_skills"] == [
        {"slug": "i405-acceptance", "current": "1.1.0", "latest": "1.2.0"}
    ]
    assert "loopskill_sync" in status["hint"]

    # unpin restores tracking.
    unpinned = loopskill_sync(db, cookbook_id=str(cb_id), unpin=True, ctx=ctx)
    assert unpinned["applied"] is True
    assert _installed(db, ctx, cb_id, "i405-acceptance") == "1.2.0"
    assert _locked(db, cb_id, "i405-acceptance") == "1.2.0"
    assert get_bundle_status(db, owner.id) is None
