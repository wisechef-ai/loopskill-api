"""evergreen_0206 Phase I — drift observability (read-only, reuses telemetry)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, FleetPing, ReconcileEvent
from app.services.drift_observability import (
    cookbook_drift_status,
    fleet_liveness,
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


def _event(db, cb_id, skill_id, semver, outcome, *, ago_hours=0):
    db.add(
        ReconcileEvent(
            bundle_id=cb_id,
            skill_id=skill_id,
            semver=semver,
            channel="canary",
            outcome=outcome,
            created_at=datetime.now(timezone.utc) - timedelta(hours=ago_hours),
        )
    )
    db.flush()


class TestCookbookDriftStatus:
    def test_healthy_cookbook(self, db):
        cb = uuid4()
        sk = uuid4()
        _event(db, cb, sk, "1.0.0", "success")
        db.commit()

        status = cookbook_drift_status(db, cb)
        assert status.success_count == 1
        assert status.failure_count == 0
        assert status.to_dict()["healthy"] is True
        assert status.last_reconcile_at is not None
        assert status.last_rollback_at is None

    def test_rollback_surfaces(self, db):
        cb = uuid4()
        sk = uuid4()
        _event(db, cb, sk, "2.0.0", "rolled_back")
        _event(db, cb, sk, "1.0.0", "success")
        db.commit()

        status = cookbook_drift_status(db, cb)
        assert status.failure_count == 1
        assert status.last_rollback_at is not None
        assert status.to_dict()["healthy"] is False
        # The failing version is surfaced.
        fv = status.failing_versions
        assert len(fv) == 1
        assert fv[0]["semver"] == "2.0.0"
        assert fv[0]["count"] == 1

    def test_old_events_outside_window_excluded(self, db):
        cb = uuid4()
        sk = uuid4()
        _event(db, cb, sk, "1.0.0", "rolled_back", ago_hours=24 * 30)  # 30 days ago
        db.commit()

        status = cookbook_drift_status(db, cb, window_days=7)
        assert status.failure_count == 0, "events older than the window must be excluded"

    def test_isolation_only_this_cookbook(self, db):
        """Status reads only the requested cookbook's events (no cross-tenant)."""
        cb_a, cb_b = uuid4(), uuid4()
        sk = uuid4()
        _event(db, cb_a, sk, "1.0.0", "success")
        _event(db, cb_b, sk, "1.0.0", "rolled_back")
        db.commit()

        status_a = cookbook_drift_status(db, cb_a)
        assert status_a.success_count == 1
        assert status_a.failure_count == 0, "cb_a must not see cb_b's rollback"


class TestFleetLiveness:
    def test_counts_distinct_recent_agents(self, db):
        today = datetime.now(timezone.utc).date()
        db.add(FleetPing(salt_hash=b"agent-1", last_seen_day=today))
        db.add(FleetPing(salt_hash=b"agent-2", last_seen_day=today))
        db.add(FleetPing(salt_hash=b"agent-1", last_seen_day=today - timedelta(days=1)))
        db.commit()

        result = fleet_liveness(db, window_days=7)
        assert result["distinct_agents_seen"] == 2, "distinct salt_hash count"

    def test_stale_agents_excluded(self, db):
        old_day = (datetime.now(timezone.utc) - timedelta(days=30)).date()
        db.add(FleetPing(salt_hash=b"stale-agent", last_seen_day=old_day))
        db.commit()

        result = fleet_liveness(db, window_days=7)
        assert result["distinct_agents_seen"] == 0, "agents outside window not counted"
