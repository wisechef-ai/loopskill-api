"""paywall_0925 — every sync surface records a (user, day) sync day.

Wiring tests for ``record_sync_day`` at its three production call sites:

* POST /api/bundles/{id}/reconcile — 200 AND the cheap 304 poll
* the ``loopskill_sync`` MCP verb (no-op sync included)
* POST /api/sync-report — attributed to the member key's owning user

plus the negative: an unauthorized reconcile (404) records nothing.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.models import APIKey, Bundle, Fleet, User, UserSyncDay
from app.services import sync_activity


@pytest.fixture(autouse=True)
def _fresh_seen_cache():
    sync_activity._clear_seen_cache()
    yield
    sync_activity._clear_seen_cache()


@pytest.fixture
def app_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    return TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))


def _user(db, tier="free"):
    u = User(
        id=uuid.uuid4(),
        display_name="sync-day",
        email=f"sd-{uuid.uuid4().hex[:10]}@example.org",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _key(db, user):
    raw = f"rec_live_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:12],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name="sync-day",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _bundle(db, owner):
    b = Bundle(id=uuid.uuid4(), name="sd", slug=f"sd-{uuid.uuid4().hex[:10]}", bundle_owner=owner.id)
    db.add(b)
    db.flush()
    return b


def _days(db, user):
    return db.query(UserSyncDay).filter(UserSyncDay.user_id == user.id).all()


class TestReconcileRecordsSyncDay:
    def test_200_then_304_is_one_sync_day(self, app_client, db_session):
        u = _user(db_session)
        k = _key(db_session, u)
        b = _bundle(db_session, u)
        db_session.commit()

        r = app_client.post(
            f"/api/bundles/{b.id}/reconcile", json={"local": [], "dry_run": True}, headers={"x-api-key": k}
        )
        assert r.status_code == 200, r.text
        etag = r.headers["etag"]
        r2 = app_client.post(
            f"/api/bundles/{b.id}/reconcile",
            json={"local": [], "dry_run": True},
            headers={"x-api-key": k, "If-None-Match": etag},
        )
        assert r2.status_code == 304, r2.text

        rows = _days(db_session, u)
        assert len(rows) == 1
        assert rows[0].first_source == sync_activity.SOURCE_RECONCILE

    def test_304_poll_alone_counts(self, app_client, db_session):
        u = _user(db_session)
        k = _key(db_session, u)
        b = _bundle(db_session, u)
        db_session.commit()
        etag = app_client.post(
            f"/api/bundles/{b.id}/reconcile", json={"local": [], "dry_run": True}, headers={"x-api-key": k}
        ).headers["etag"]
        db_session.query(UserSyncDay).delete()
        db_session.commit()
        sync_activity._clear_seen_cache()

        r = app_client.post(
            f"/api/bundles/{b.id}/reconcile",
            json={"local": [], "dry_run": True},
            headers={"x-api-key": k, "If-None-Match": etag},
        )
        assert r.status_code == 304
        assert len(_days(db_session, u)) == 1

    def test_unauthorized_reconcile_records_nothing(self, app_client, db_session):
        owner, intruder = _user(db_session), _user(db_session)
        k = _key(db_session, intruder)
        b = _bundle(db_session, owner)
        db_session.commit()
        r = app_client.post(
            f"/api/bundles/{b.id}/reconcile", json={"local": [], "dry_run": True}, headers={"x-api-key": k}
        )
        assert r.status_code == 404, r.text
        assert _days(db_session, intruder) == [] and _days(db_session, owner) == []


class TestMcpSyncRecordsSyncDay:
    def test_noop_sync_counts(self, db_session):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.loopskill_sync import loopskill_sync

        u = _user(db_session)
        b = _bundle(db_session, u)
        db_session.commit()
        out = loopskill_sync(
            db_session, cookbook_id=str(b.id), ctx=AuthContext(scope="user", user_id=u.id, tier="free")
        )
        assert out["changes"] == []
        rows = _days(db_session, u)
        assert len(rows) == 1 and rows[0].first_source == sync_activity.SOURCE_MCP_SYNC

    def test_not_found_records_nothing(self, db_session):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.loopskill_sync import loopskill_sync

        owner, other = _user(db_session), _user(db_session)
        b = _bundle(db_session, owner)
        db_session.commit()
        out = loopskill_sync(
            db_session, cookbook_id=str(b.id), ctx=AuthContext(scope="user", user_id=other.id)
        )
        assert out["error"] == "not_found"
        assert _days(db_session, other) == []


class TestSyncReportRecordsSyncDay:
    def test_member_report_attributed_to_owner(self, app_client, db_session):
        owner = _user(db_session, "pro")
        owner_key = _key(db_session, owner)
        fleet = Fleet(
            id=uuid.uuid4(),
            owner_user_id=owner.id,
            name="sd-fleet",
            fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
        )
        db_session.add(fleet)
        db_session.commit()
        enr = app_client.post(
            f"/api/fleets/{fleet.id}/members",
            headers={"x-api-key": owner_key},
            json={"host": "h", "profile": "default", "skills_dir": "~/.hermes/loopskill"},
        )
        assert enr.status_code == 201, enr.text
        member_key = enr.json()["api_key"]

        r = app_client.post(
            "/api/sync-report", headers={"x-api-key": member_key}, json={"cycle_ts": "2026-09-25T10:00:00Z"}
        )
        assert r.status_code == 200, r.text
        rows = _days(db_session, owner)
        assert len(rows) == 1 and rows[0].first_source == sync_activity.SOURCE_SYNC_REPORT
