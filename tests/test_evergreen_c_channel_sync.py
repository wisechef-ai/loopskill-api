"""evergreen_0206 Phase C — channel-aware fleet sync.

Pins the contract that channels are REAL (recon: they were inert labels):
  canary → advances to latest semver
  stable → advances only to versions that passed the gate (promoted_to_stable_at)
  frozen → never moves

The headline test: the SAME cookbook on 3 channels diverges correctly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.models import (
    Base,
    Bundle,
    BundleSkill,
    Fleet,
    FleetSubscription,
    Skill,
    SkillVersion,
    User,
)
from app.services.channel_select import latest_version_for_channel
from app.services.fleet_sync import sync_fleet


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
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ─────────────────────────── Helpers ────────────────────────────────────


def _user(db: Session) -> User:
    uid = uuid4()
    u = User(
        id=uid,
        display_name="Fleet Owner",
        email=f"{uid}@test.example",
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _skill_with_versions(db: Session, slug: str, versions: list[tuple[str, bool]]) -> Skill:
    """versions: list of (semver, promoted). promoted=True → stable-eligible."""
    s = Skill(id=uuid4(), slug=slug, title=slug, description="x", is_public=True)
    db.add(s)
    db.flush()
    for semver, promoted in versions:
        db.add(
            SkillVersion(
                id=uuid4(),
                skill_id=s.id,
                semver=semver,
                tarball_path=f"/tmp/{slug}-{semver}.tar.gz",
                tarball_size_bytes=10,
                checksum_sha256="a" * 64,
                promoted_to_stable_at=(datetime(2026, 1, 1, tzinfo=timezone.utc) if promoted else None),
            )
        )
    db.flush()
    return s


def _cookbook_with_skill(db: Session, owner: User, skill: Skill, pin: str) -> Bundle:
    cb = Bundle(id=uuid4(), name="CB", is_base=False, bundle_owner=owner.id)
    db.add(cb)
    db.flush()
    db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="overridden", pinned_version=pin))
    db.flush()
    return cb


def _fleet(db: Session, owner: User) -> Fleet:
    f = Fleet(
        id=uuid4(),
        name="F",
        owner_user_id=owner.id,
        fleet_api_key_hash=uuid4().hex + uuid4().hex,  # dummy 64-char hash
    )
    db.add(f)
    db.flush()
    return f


# ─────────────────────── channel_select primitive ───────────────────────


class TestChannelSelect:
    def test_canary_picks_latest_any(self, db):
        s = _skill_with_versions(db, "cs-canary", [("1.0.0", True), ("2.0.0", False)])
        db.commit()
        # canary → 2.0.0 even though it's unpromoted
        assert latest_version_for_channel(db, s.id, "canary") == "2.0.0"

    def test_stable_picks_latest_promoted(self, db):
        s = _skill_with_versions(db, "cs-stable", [("1.0.0", True), ("2.0.0", False)])
        db.commit()
        # stable → 1.0.0 (the only promoted one); 2.0.0 not yet gated
        assert latest_version_for_channel(db, s.id, "stable") == "1.0.0"

    def test_stable_none_when_nothing_promoted(self, db):
        s = _skill_with_versions(db, "cs-nopromote", [("1.0.0", False)])
        db.commit()
        assert latest_version_for_channel(db, s.id, "stable") is None

    def test_frozen_returns_none(self, db):
        s = _skill_with_versions(db, "cs-frozen", [("1.0.0", True)])
        db.commit()
        assert latest_version_for_channel(db, s.id, "frozen") is None


# ─────────────────────── The headline divergence test ───────────────────


class TestThreeChannelDivergence:
    def test_same_cookbook_three_channels_diverge(self, db):
        """canary advances to latest, stable to latest-promoted, frozen holds."""
        owner = _user(db)
        # 1.0.0 promoted (stable-eligible), 2.0.0 NOT promoted (canary-only).
        skill = _skill_with_versions(db, "div-skill", [("1.0.0", True), ("2.0.0", False)])

        # Three cookbooks, each pinned at 1.0.0, one per channel.
        cb_canary = _cookbook_with_skill(db, owner, skill, pin="1.0.0")
        cb_stable = _cookbook_with_skill(db, owner, skill, pin="1.0.0")
        cb_frozen = _cookbook_with_skill(db, owner, skill, pin="1.0.0")

        fleet = _fleet(db, owner)
        db.add_all(
            [
                FleetSubscription(fleet_id=fleet.id, bundle_id=cb_canary.id, channel="canary"),
                FleetSubscription(fleet_id=fleet.id, bundle_id=cb_stable.id, channel="stable"),
                FleetSubscription(fleet_id=fleet.id, bundle_id=cb_frozen.id, channel="frozen"),
            ]
        )
        db.commit()

        ctx = AuthContext(scope="master")
        results = sync_fleet(db, fleet.id, dry_run=False, ctx=ctx)
        by_cb = {r["cookbook_id"]: r for r in results}

        # CANARY: advances 1.0.0 → 2.0.0 (latest, unpromoted OK)
        canary = by_cb[str(cb_canary.id)]
        assert canary["channel"] == "canary"
        assert canary["changes"] == [
            {"slug": "div-skill", "from": "1.0.0", "to": "2.0.0", "action": "update"}
        ]
        assert canary["applied"] is True

        # STABLE: already at 1.0.0 (the latest promoted) → no change
        stable = by_cb[str(cb_stable.id)]
        assert stable["channel"] == "stable"
        assert stable["changes"] == [], "stable must NOT advance to unpromoted 2.0.0"

        # FROZEN: never moves
        frozen = by_cb[str(cb_frozen.id)]
        assert frozen["channel"] == "frozen"
        assert frozen["frozen"] is True
        assert frozen["changes"] == []
        assert frozen["applied"] is False

        # Verify the actual DB pins
        db.expire_all()
        canary_pin = (
            db.query(BundleSkill).filter(BundleSkill.bundle_id == cb_canary.id).first().pinned_version
        )
        stable_pin = (
            db.query(BundleSkill).filter(BundleSkill.bundle_id == cb_stable.id).first().pinned_version
        )
        frozen_pin = (
            db.query(BundleSkill).filter(BundleSkill.bundle_id == cb_frozen.id).first().pinned_version
        )
        assert canary_pin == "2.0.0"
        assert stable_pin == "1.0.0"
        assert frozen_pin == "1.0.0"

    def test_stable_advances_after_promotion(self, db):
        """Once 2.0.0 is promoted, a stable subscription advances to it."""
        owner = _user(db)
        skill = _skill_with_versions(db, "promote-skill", [("1.0.0", True), ("2.0.0", True)])
        cb = _cookbook_with_skill(db, owner, skill, pin="1.0.0")
        fleet = _fleet(db, owner)
        db.add(FleetSubscription(fleet_id=fleet.id, bundle_id=cb.id, channel="stable"))
        db.commit()

        ctx = AuthContext(scope="master")
        results = sync_fleet(db, fleet.id, dry_run=False, ctx=ctx)
        assert results[0]["changes"][0]["to"] == "2.0.0", "stable must advance to 2.0.0 once it's promoted"


class TestFleetSyncDryRun:
    def test_dry_run_reports_without_writing(self, db):
        owner = _user(db)
        skill = _skill_with_versions(db, "dry-skill", [("1.0.0", False), ("2.0.0", False)])
        cb = _cookbook_with_skill(db, owner, skill, pin="1.0.0")
        fleet = _fleet(db, owner)
        db.add(FleetSubscription(fleet_id=fleet.id, bundle_id=cb.id, channel="canary"))
        db.commit()

        ctx = AuthContext(scope="master")
        results = sync_fleet(db, fleet.id, dry_run=True, ctx=ctx)
        assert results[0]["changes"][0]["to"] == "2.0.0"
        assert results[0]["applied"] is False
        # No write
        db.expire_all()
        pin = db.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id).first().pinned_version
        assert pin == "1.0.0", "dry_run must not write"
