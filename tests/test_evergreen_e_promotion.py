"""evergreen_0206 Phase E — health/eval-gated canary→stable promotion.

The headline gate: a bad version on canary (triggered a rollback on a canary
agent) is BLOCKED from promoting to stable — stable agents never receive it. A
clean version passes the gate after the observation window and becomes available
to stable agents. frozen never advances.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, ReconcileEvent, Skill, SkillVersion
from app.services.channel_select import latest_version_for_channel
from app.services.promotion import (
    GateConfig,
    evaluate_gate,
    promote_if_eligible,
    record_reconcile_event,
)


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _r):
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


def _skill_version(db: Session, slug: str, semver: str) -> tuple[Skill, SkillVersion]:
    s = Skill(id=uuid4(), slug=slug, title=slug, description="x", is_public=True)
    db.add(s)
    db.flush()
    v = SkillVersion(
        id=uuid4(),
        skill_id=s.id,
        semver=semver,
        tarball_path=f"/tmp/{slug}-{semver}.tar.gz",
        tarball_size_bytes=10,
        checksum_sha256="a" * 64,
    )
    db.add(v)
    db.flush()
    return s, v


# ─────────────────────────── The headline gate ──────────────────────────


class TestPromotionGate:
    def test_bad_version_blocked_from_stable(self, db):
        """A canary rollback BLOCKS promotion — stable never gets the bad version."""
        s, v = _skill_version(db, "bad-skill", "2.0.0")
        # A canary agent rolled this version back.
        record_reconcile_event(db, skill_id=s.id, semver="2.0.0", outcome="rolled_back", api_key_id=uuid4())
        db.commit()

        result = promote_if_eligible(db, s.id, "2.0.0")
        assert result.promotable is False
        assert "blocked" in result.reason
        # promoted_to_stable_at NOT set → Phase C stable selection won't pick it.
        db.expire_all()
        v2 = db.query(SkillVersion).filter(SkillVersion.id == v.id).first()
        assert v2.promoted_to_stable_at is None
        # And stable-channel selection refuses it.
        assert latest_version_for_channel(db, s.id, "stable") is None

    def test_clean_version_promotes_after_window(self, db):
        """A version with only successful canary reconciles passes the gate."""
        s, v = _skill_version(db, "good-skill", "1.0.0")
        record_reconcile_event(db, skill_id=s.id, semver="1.0.0", outcome="success", api_key_id=uuid4())
        db.commit()

        result = promote_if_eligible(db, s.id, "1.0.0")
        assert result.promotable is True
        assert result.reason == "gate passed"
        db.expire_all()
        v2 = db.query(SkillVersion).filter(SkillVersion.id == v.id).first()
        assert v2.promoted_to_stable_at is not None
        # Now stable-channel selection picks it.
        assert latest_version_for_channel(db, s.id, "stable") == "1.0.0"

    def test_no_telemetry_blocks(self, db):
        """No canary reconciles observed → insufficient successes → blocked."""
        s, v = _skill_version(db, "untested", "1.0.0")
        db.commit()
        result = promote_if_eligible(db, s.id, "1.0.0")
        assert result.promotable is False
        assert "insufficient" in result.reason

    def test_failure_outside_window_ignored(self, db):
        """A failure older than the observation window does not block."""
        s, v = _skill_version(db, "old-fail", "1.0.0")
        # Old failure (48h ago), recent success.
        old = ReconcileEvent(
            skill_id=s.id,
            semver="1.0.0",
            channel="canary",
            outcome="rolled_back",
            api_key_id=uuid4(),
            created_at=datetime.now(timezone.utc) - timedelta(hours=48),
        )
        db.add(old)
        record_reconcile_event(db, skill_id=s.id, semver="1.0.0", outcome="success", api_key_id=uuid4())
        db.commit()

        # Default 24h window → old failure excluded → promotable.
        result = promote_if_eligible(db, s.id, "1.0.0")
        assert result.promotable is True, "a failure older than the window must not block"

    def test_idempotent_no_double_promote(self, db):
        s, v = _skill_version(db, "idem-skill", "1.0.0")
        record_reconcile_event(db, skill_id=s.id, semver="1.0.0", outcome="success", api_key_id=uuid4())
        db.commit()

        r1 = promote_if_eligible(db, s.id, "1.0.0")
        first_ts = db.query(SkillVersion).filter(SkillVersion.id == v.id).first().promoted_to_stable_at
        r2 = promote_if_eligible(db, s.id, "1.0.0")
        second_ts = db.query(SkillVersion).filter(SkillVersion.id == v.id).first().promoted_to_stable_at
        assert r1.promotable and r2.reason == "already_promoted"
        assert first_ts == second_ts, "re-promotion must not re-stamp the timestamp"


# ─────────────────────────── eval.yaml tightening ───────────────────────


class TestEvalYamlGate:
    def test_declared_gate_requires_min_distinct_agents(self, db):
        """eval.yaml can require N distinct canary agents (tighter than default)."""
        s, v = _skill_version(db, "strict-skill", "1.0.0")
        # Two successes but from the SAME agent.
        agent = uuid4()
        record_reconcile_event(db, skill_id=s.id, semver="1.0.0", outcome="success", api_key_id=agent)
        record_reconcile_event(db, skill_id=s.id, semver="1.0.0", outcome="success", api_key_id=agent)
        db.commit()

        cfg = GateConfig.from_eval_yaml({"promotion_gate": {"min_distinct_agents": 2}})
        result = evaluate_gate(db, s.id, "1.0.0", config=cfg)
        assert result.promotable is False
        assert "distinct" in result.reason

    def test_declared_gate_passes_with_two_agents(self, db):
        s, v = _skill_version(db, "twoagent-skill", "1.0.0")
        record_reconcile_event(db, skill_id=s.id, semver="1.0.0", outcome="success", api_key_id=uuid4())
        record_reconcile_event(db, skill_id=s.id, semver="1.0.0", outcome="success", api_key_id=uuid4())
        db.commit()

        cfg = GateConfig.from_eval_yaml({"promotion_gate": {"min_distinct_agents": 2}})
        result = evaluate_gate(db, s.id, "1.0.0", config=cfg)
        assert result.promotable is True

    def test_default_config_when_no_yaml(self):
        cfg = GateConfig.from_eval_yaml(None)
        assert cfg.observation_hours == 24
        assert cfg.min_success == 1
        assert cfg.min_distinct_agents == 1


# ─────────────────────────── Frozen never advances ──────────────────────


class TestFrozenNeverAdvances:
    def test_promotion_does_not_touch_frozen_selection(self, db):
        """Even a promoted version: frozen-channel selection still returns None."""
        s, v = _skill_version(db, "frozen-skill", "1.0.0")
        record_reconcile_event(db, skill_id=s.id, semver="1.0.0", outcome="success", api_key_id=uuid4())
        db.commit()
        promote_if_eligible(db, s.id, "1.0.0")
        # Frozen holds regardless of promotion state.
        assert latest_version_for_channel(db, s.id, "frozen") is None
