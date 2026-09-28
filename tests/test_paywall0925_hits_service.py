"""paywall_0925 — record_paywall_hit / record_sync_day / pulse aggregates (unit)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models import PaywallHit, User, UserSyncDay
from app.services import sync_activity
from app.services.paywall_hits import GATE_API_KEY_CAP, GATE_DEPLOY_TIER, record_paywall_hit
from app.services.paywall_pulse import paywall_hits_by_gate, repeat_sync_users
from app.services.sync_activity import SOURCE_RECONCILE, record_sync_day


@pytest.fixture(autouse=True)
def _fresh_seen_cache():
    sync_activity._clear_seen_cache()
    yield
    sync_activity._clear_seen_cache()


def _user(db, *, email=None, is_agent=False):
    u = User(
        id=uuid4(),
        email=email or f"u-{uuid4().hex[:8]}@example.org",
        display_name="paywall test user",
        subscription_tier="free",
        is_agent=is_agent,
    )
    db.add(u)
    db.commit()
    return u


def _hits(db, **filters):
    return db.query(PaywallHit).filter_by(**filters).all()


class TestRecordPaywallHit:
    def test_first_hit_writes_one_row_classified_stranger(self, db_session):
        u = _user(db_session)
        record_paywall_hit(db_session, gate=GATE_DEPLOY_TIER, http_status=402, tier="free", user_id=u.id)
        rows = _hits(db_session, user_id=u.id)
        assert len(rows) == 1
        r = rows[0]
        assert (r.gate, r.http_status, r.tier, r.hit_count) == (GATE_DEPLOY_TIER, 402, "free", 1)
        assert r.subject_key == f"user:{u.id}"
        assert r.classification == "stranger"
        assert r.classification_evidence

    def test_repeat_same_day_bumps_count_not_rows(self, db_session):
        u = _user(db_session)
        for _ in range(3):
            record_paywall_hit(db_session, gate=GATE_DEPLOY_TIER, http_status=402, tier="free", user_id=u.id)
        rows = _hits(db_session, user_id=u.id)
        assert len(rows) == 1
        db_session.refresh(rows[0])
        assert rows[0].hit_count == 3

    def test_new_day_or_new_gate_is_a_new_row(self, db_session):
        u = _user(db_session)
        now = datetime.now(UTC)
        record_paywall_hit(
            db_session, gate=GATE_DEPLOY_TIER, http_status=402, tier="free", user_id=u.id, now=now
        )
        record_paywall_hit(
            db_session,
            gate=GATE_DEPLOY_TIER,
            http_status=402,
            tier="free",
            user_id=u.id,
            now=now - timedelta(days=1),
        )
        record_paywall_hit(
            db_session, gate=GATE_API_KEY_CAP, http_status=403, tier="free", user_id=u.id, now=now
        )
        assert len(_hits(db_session, user_id=u.id)) == 3

    def test_fleet_email_is_classified_fleet(self, db_session):
        from app.services.funnel_ledger import fleet_exclusions

        emails = sorted(fleet_exclusions()["emails"])
        assert emails, "config/fleet_exclusions.yaml lists no fleet emails"
        u = _user(db_session, email=emails[0])
        record_paywall_hit(db_session, gate=GATE_DEPLOY_TIER, http_status=402, tier="free", user_id=u.id)
        assert _hits(db_session, user_id=u.id)[0].classification == "fleet"

    def test_never_raises_on_db_failure(self, db_session, monkeypatch):
        import app.services.paywall_hits as ph

        def boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(ph, "_record", boom)
        # Must swallow: the caller is about to raise its own 402/403.
        record_paywall_hit(db_session, gate=GATE_DEPLOY_TIER, http_status=402, tier="free", user_id=uuid4())

    def test_row_survives_caller_rollback(self, tmp_path):
        """The gate raises right after recording and the request session is
        discarded uncommitted. The hit must survive that — and must NOT commit
        the handler's own pending work.

        Uses a standalone Engine (the production bind shape): the per-test
        db_session fixture binds to one shared Connection, where every write
        nests inside the fixture's savepoint by design.
        """
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as _S

        eng = create_engine(f"sqlite:///{tmp_path / 'side.db'}")
        PaywallHit.__table__.create(eng)
        UserSyncDay.__table__.create(eng)
        request_db = _S(bind=eng, autoflush=False)
        # Handler has pending, uncommitted work. (Left unflushed only because
        # SQLite is single-writer: a flushed write would hold the file lock the
        # side session needs. Postgres has no such lock between rows.)
        request_db.add(UserSyncDay(user_id=uuid4(), day=datetime.now(UTC).date(), first_source="x"))
        record_paywall_hit(
            request_db,
            gate=GATE_DEPLOY_TIER,
            http_status=402,
            tier="free",
            ip="203.0.113.7",
            email="someone@example.org",
        )
        request_db.rollback()  # the HTTPException path: never committed
        request_db.close()

        with _S(bind=eng) as check:
            assert check.query(PaywallHit).count() == 1  # telemetry survived
            assert check.query(UserSyncDay).count() == 0  # handler work did NOT leak
        eng.dispose()

    def test_anonymous_ip_subject(self, db_session):
        record_paywall_hit(db_session, gate=GATE_DEPLOY_TIER, http_status=402, tier=None, ip="203.0.113.9")
        rows = _hits(db_session, subject_key="ip:203.0.113.9")
        assert len(rows) == 1 and rows[0].user_id is None


class TestRecordSyncDay:
    def test_idempotent_per_user_day(self, db_session):
        u = _user(db_session)
        for _ in range(3):
            record_sync_day(db_session, u.id, source=SOURCE_RECONCILE)
        sync_activity._clear_seen_cache()  # also idempotent without the cache
        record_sync_day(db_session, u.id, source=SOURCE_RECONCILE)
        assert db_session.query(UserSyncDay).filter_by(user_id=u.id).count() == 1

    def test_none_user_is_noop(self, db_session):
        before = db_session.query(UserSyncDay).count()
        record_sync_day(db_session, None, source=SOURCE_RECONCILE)
        assert db_session.query(UserSyncDay).count() == before

    def test_cache_hit_issues_no_query(self, db_session, monkeypatch):
        u = _user(db_session)
        record_sync_day(db_session, u.id, source=SOURCE_RECONCILE)

        def fail(*a, **k):
            raise AssertionError("second same-day call must not touch the DB")

        monkeypatch.setattr(sync_activity, "_write", fail)
        record_sync_day(db_session, u.id, source=SOURCE_RECONCILE)


class TestPulseAggregates:
    def test_hits_by_gate_counts_strangers_and_repeats(self, db_session):
        a, b = _user(db_session), _user(db_session)
        for _ in range(2):
            record_paywall_hit(db_session, gate=GATE_DEPLOY_TIER, http_status=402, tier="free", user_id=a.id)
        record_paywall_hit(db_session, gate=GATE_DEPLOY_TIER, http_status=402, tier="free", user_id=b.id)
        old = datetime.now(UTC) - timedelta(days=10)
        record_paywall_hit(
            db_session, gate=GATE_API_KEY_CAP, http_status=403, tier="free", user_id=a.id, now=old
        )

        today = datetime.now(UTC).date()
        out = {g.gate: g for g in paywall_hits_by_gate(db_session, since=today - timedelta(days=6))}
        assert set(out) == {GATE_DEPLOY_TIER}  # the 10-day-old hit is outside the window
        d = out[GATE_DEPLOY_TIER]
        assert (d.hits, d.subjects, d.stranger_subjects) == (3, 2, 2)

    def test_repeat_sync_users_needs_two_distinct_days_and_humans(self, db_session):
        today = datetime.now(UTC)
        once, twice, agent = _user(db_session), _user(db_session), _user(db_session, is_agent=True)
        record_sync_day(db_session, once.id, source=SOURCE_RECONCILE, now=today)
        for d in (0, 3):
            record_sync_day(db_session, twice.id, source=SOURCE_RECONCILE, now=today - timedelta(days=d))
            record_sync_day(db_session, agent.id, source=SOURCE_RECONCILE, now=today - timedelta(days=d))
        # a second sync day 40 days ago does not make `once` a repeat user
        record_sync_day(db_session, once.id, source=SOURCE_RECONCILE, now=today - timedelta(days=40))
        assert repeat_sync_users(db_session, today=today.date()) == 1
