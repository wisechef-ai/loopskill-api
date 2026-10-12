"""loopskill-api#407 — ONE definition of "latest", CalVer-compatible; no half pins.

Before this change "latest" had two meanings:

* install (MCP ``_resolve_version``, REST ``/api/skills/install``, bundle apply,
  ``Skill.versions[0]`` in a dozen display sites) used *upload order*
  (``created_at`` desc);
* ``loopskill_sync`` / ``bundle_status`` / drift used the highest version
  number (``semver_key``).

So publish ``2.0.0`` then backport ``1.4.3``: an unpinned install served 1.4.3
while sync pinned 2.0.0. And ``reconcile`` (apply) / ``fleet_sync`` wrote
``pinned_version`` without ``pin_mode``: install honoured the pin, the lock
resolver (which reads ``pin_mode``) kept following the head.

Owner decision (Adam, 2026-10-12): "latest" follows calendar versioning. The
ordering is numeric component-by-component (``semver_key``), which already ranks
``2026.10.1`` above any legacy ``2.x.y`` — upload order is never consulted
except as a tiebreak between rows whose version numbers are equal.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from datetime import datetime, timedelta
from typing import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.mcp.bundle_status import get_bundle_status
from app.mcp.tools.bundle_install import loopskill_bundle_install
from app.mcp.tools.loopskill_sync import loopskill_sync
from app.models import Base, Bundle, BundleSkill, Fleet, FleetSubscription, Skill, SkillVersion, User
from app.services.drift_service import resolve_bundle_entries
from app.services.fleet_sync import sync_fleet
from app.services.reconcile import recipes_reconcile
from app.services.semver import latest_of, latest_version_row_for_skill, semver_key

_T0 = datetime(2026, 1, 1, 12, 0, 0)
_REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
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
    """A version uploaded ``minutes`` after T0 (upload order ≠ version order on purpose)."""
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
    cb = Bundle(id=uuid.uuid4(), name="B407", bundle_owner=owner.id)
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


# ── 1. the ordering itself ──────────────────────────────────────────────────


def test_calver_outranks_legacy_semver_and_orders_numerically():
    assert semver_key("2026.10.1") > semver_key("2.0.0")
    assert semver_key("2026.10.12") > semver_key("2026.10.2"), "numeric, not lexical"
    assert semver_key("2026.11.1") > semver_key("2026.10.31")
    assert semver_key("2027.1.1") > semver_key("2026.12.31")


def test_latest_of_ignores_upload_order_and_uses_created_at_only_as_tiebreak(db):
    s = _skill(db, "i407-order")
    _publish(db, s, "2.0.0", 0)
    _publish(db, s, "1.4.3", 1)  # backport uploaded AFTER 2.0.0
    assert latest_version_row_for_skill(db, s.id).semver == "2.0.0"
    db.refresh(s)
    assert s.versions[0].semver == "1.4.3", "premise: versions[] is upload order"
    assert s.latest_version_row.semver == "2.0.0"

    t = _skill(db, "i407-tie")
    _publish(db, t, "1.2.3", 0)
    newer = _publish(db, t, "v1.2.3", 5)  # equal key → the later upload wins
    assert latest_version_row_for_skill(db, t.id).id == newer.id


def test_latest_of_empty_and_unparseable():
    assert latest_of([]) is None

    class _V:
        def __init__(self, semver, created_at=None):
            self.semver, self.created_at = semver, created_at

    junk, real = _V("garbage"), _V("0.0.1")
    assert latest_of([junk, real]) is real, "a malformed row never out-sorts a real version"
    assert latest_of([_V("1.0.0", None), _V("1.0.0", _T0)]).created_at == _T0, (
        "NULL created_at must not crash"
    )


def test_hostile_version_strings_floor_instead_of_raising():
    """Breaker attack: "latest" now runs on every install — it must never 500."""
    assert semver_key("1." + "1" * 100_000) == (-1, 0, 0, 0)
    assert semver_key("v" + "9" * 50_000) == (-1, 0, 0, 0)
    assert semver_key("1.0.0'; DROP TABLE skills;--") == (-1, 0, 0, 0)
    assert semver_key("1.2.3-" + "-" * 100_000)[:3] == (1, 2, 3)


def test_empty_skill_has_no_latest(db):
    s = _skill(db, "i407-empty")
    assert latest_version_row_for_skill(db, s.id) is None
    db.refresh(s)
    assert s.latest_version_row is None


# ── 2. acceptance: install = sync pin = bundle_status latest = 2.0.0 ─────────


def test_acceptance_unpinned_install_equals_sync_pin_equals_status_latest(db):
    owner = _user(db)
    ctx = _ctx(owner)
    s = _skill(db, "i407-accept")
    _publish(db, s, "1.0.0", 0)
    _publish(db, s, "2.0.0", 1)
    _publish(db, s, "1.4.3", 2)  # the backport that used to hijack "latest"
    cb = _bundle(db, owner, (s, None))

    assert _installed(db, ctx, cb.id, s.slug) == "2.0.0", (
        "unpinned install = highest version, not last upload"
    )

    out = loopskill_sync(db, cookbook_id=str(cb.id), ctx=ctx)
    assert out["applied"] is True
    assert _row(db, cb.id, s).pinned_version == "2.0.0"
    assert _installed(db, ctx, cb.id, s.slug) == "2.0.0"
    assert _locked(db, cb.id, s.slug) == "2.0.0"

    # bundle_status "latest" for a pinned-behind row is the same 2.0.0
    row = _row(db, cb.id, s)
    row.pinned_version, row.pin_mode = "1.0.0", "pin"
    db.commit()
    status = get_bundle_status(db, owner.id)
    assert status is not None
    (entry,) = status["your_cookbooks"]
    assert [o["latest"] for o in entry["outdated_skills"]] == ["2.0.0"]


def test_calver_release_becomes_latest_everywhere(db):
    owner = _user(db)
    ctx = _ctx(owner)
    s = _skill(db, "i407-calver")
    _publish(db, s, "2.0.0", 0)
    _publish(db, s, "2026.10.1", 1)
    _publish(db, s, "1.4.3", 2)
    cb = _bundle(db, owner, (s, None))

    assert _installed(db, ctx, cb.id, s.slug) == "2026.10.1"
    loopskill_sync(db, cookbook_id=str(cb.id), ctx=ctx)
    assert _row(db, cb.id, s).pinned_version == "2026.10.1"
    assert _locked(db, cb.id, s.slug) == "2026.10.1"


# ── 3. half pins: reconcile / fleet-sync write pin_mode with pinned_version ──


def test_reconcile_apply_writes_a_full_pin(db):
    owner = _user(db)
    ctx = _ctx(owner)
    s = _skill(db, "i407-reconcile")
    _publish(db, s, "1.0.0", 0)
    _publish(db, s, "2.0.0", 1)
    cb = _bundle(db, owner, (s, None))

    out = recipes_reconcile(
        db,
        cookbook_id=str(cb.id),
        local=[{"slug": s.slug, "pinned_version": "1.0.0"}],
        dry_run=False,
        ctx=ctx,
    )
    assert out["diff"]["update"], "premise: reconcile planned an update"
    row = _row(db, cb.id, s)
    assert (row.pinned_version, row.pin_mode) == ("2.0.0", "pin"), "pinned_version alone is a half pin"

    _publish(db, s, "3.0.0", 2)  # the head moves on; the pin must hold in BOTH resolvers
    assert _installed(db, ctx, cb.id, s.slug) == "2.0.0"
    assert _locked(db, cb.id, s.slug) == "2.0.0"


def test_fleet_sync_writes_a_full_pin(db):
    owner = _user(db)
    ctx = _ctx(owner)
    s = _skill(db, "i407-fleet")
    _publish(db, s, "1.0.0", 0)
    _publish(db, s, "2.0.0", 1)
    cb = _bundle(db, owner, (s, None))
    fleet = Fleet(
        id=uuid.uuid4(), owner_user_id=owner.id, name="f407", fleet_api_key_hash=uuid.uuid4().hex * 2
    )
    db.add(fleet)
    db.flush()
    db.add(FleetSubscription(fleet_id=fleet.id, bundle_id=cb.id, channel="canary"))
    db.commit()

    results = sync_fleet(db, fleet.id, dry_run=False, ctx=ctx)
    assert results[0]["applied"] is True
    row = _row(db, cb.id, s)
    assert (row.pinned_version, row.pin_mode) == ("2.0.0", "pin")

    _publish(db, s, "3.0.0", 2)
    assert _installed(db, ctx, cb.id, s.slug) == "2.0.0"
    assert _locked(db, cb.id, s.slug) == "2.0.0"


# ── 4. ratchet: nobody re-derives "latest" from upload order ─────────────────


def test_no_skill_latest_is_derived_from_upload_order():
    """``Skill.versions`` and ``SkillVersion.created_at.desc()`` are upload order.

    Skill "latest" must go through ``Skill.latest_version_row`` /
    ``latest_version_row_for_skill``. (Loop / Personality / Verifier have their
    own ``versions`` and are out of scope for #407.)
    """
    offenders: list[str] = []
    skill_idx = re.compile(r"\bskill\.versions\[0\]|\b[rs]\.versions\[0\]\.semver if [rs]\.versions")
    for path in sorted((_REPO / "app").rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        text = path.read_text()
        for n, line in enumerate(text.splitlines(), 1):
            if "``" in line or line.lstrip().startswith("#"):
                continue  # prose / comments, not code
            if "SkillVersion.created_at.desc()" in line and rel != "app/models.py":
                offenders.append(f"{rel}:{n}: {line.strip()}")
            if skill_idx.search(line) and rel not in {"app/mcp/tools/loopskill_catalog.py"}:
                # r/s receivers in verifier/personality/loop modules are other models
                if rel in {
                    "app/personality_routes.py",
                    "app/verifier_routes.py",
                    "app/composite_loop_routes.py",
                    "app/mcp/tools/composite_loop_catalog.py",
                }:
                    continue
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, "latest derived from upload order:\n" + "\n".join(offenders)
