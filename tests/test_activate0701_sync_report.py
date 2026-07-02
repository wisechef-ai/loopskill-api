"""Phase T (activate_0701) — BATCHED SYNC-REPORT INGESTION.

TDD tests per docs/design/activate0701-phaseT-sync-report.md §10.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_user(db, *, tier="pro"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="sync-report-owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user, *, label="owner-key"):
    from app.models import APIKey

    raw = f"rec_live_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:12],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name=label,
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _mk_fleet(db, owner):
    from app.models import Fleet

    fleet = Fleet(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        name="sync-report-fleet",
        fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
    )
    db.add(fleet)
    db.flush()
    return fleet


def _enroll_member(middleware_client, db, fleet, owner_key, *, host="agent-host", profile="default"):
    """Enroll a fleet member and return (member_id, member_key)."""
    r = middleware_client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": owner_key},
        json={"host": host, "profile": profile, "skills_dir": "~/.hermes/loopskill"},
    )
    assert r.status_code == 201, r.text
    return r.json()["member_id"], r.json()["api_key"]


# ── 1. member-key POST full payload → 200, all rows landed ───────────────────


def test_full_payload_all_rows_landed(middleware_client, db_session):
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    _, member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)

    payload = {
        "cycle_ts": "2026-07-02T21:30:00Z",
        "loop_runs": [
            {
                "loop_slug": "atomic-habits",
                "instance_key": "tori/default",
                "outcome": "success",
                "accepted_change": True,
                "cost_usd": 0.42,
                "duration_seconds": 118,
                "provenance_id": "prov-123",
                "started_at": "2026-07-02T21:00:00Z",
                "detail": "shipped one improvement",
            },
        ],
        "skill_errors": [
            {
                "slug": "broken-skill",
                "semver": "1.2.0",
                "signature": "err-sig-abc",
                "summary": "ImportError on line 5",
            }
        ],
        "cron_health": {
            "failed": [{"job_name": "daily-digest", "last_status": "error", "consecutive_failures": 3}],
            "counts": {"total": 12, "ok": 11, "error": 1},
        },
    }

    r = middleware_client.post("/api/sync-report", headers={"x-api-key": member_key}, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"]["loop_runs"] == 1
    assert body["recorded"]["skill_errors"] == 1
    assert body["recorded"]["cron_health"] is True

    from app.models import CronHealthSnapshot, LoopRun, SkillErrorReport

    from app.services.fleet_members import resolve_member_for_key
    from app.models import APIKey

    key_row = (
        db_session.query(APIKey)
        .filter(APIKey.key_hash == hashlib.sha256(member_key.encode()).hexdigest())
        .first()
    )
    member = resolve_member_for_key(db_session, key_row.id)
    assert member is not None

    lr = db_session.query(LoopRun).filter(LoopRun.member_id == member.id).all()
    assert len(lr) == 1
    assert lr[0].loop_slug == "atomic-habits"
    assert lr[0].outcome == "success"
    assert lr[0].accepted_change is True
    assert lr[0].fleet_id == fleet.id

    se = db_session.query(SkillErrorReport).filter(SkillErrorReport.member_id == member.id).all()
    assert len(se) == 1
    assert se[0].slug == "broken-skill"
    assert se[0].feedback_status == "pending"

    ch = db_session.query(CronHealthSnapshot).filter(CronHealthSnapshot.member_id == member.id).all()
    assert len(ch) == 1
    assert ch[0].total_count == 12
    assert ch[0].error_count == 1
    assert len(ch[0].failed) == 1


# ── 2. non-member key → 403; anonymous → 401 ─────────────────────────────────


def test_non_member_key_403(middleware_client, db_session):
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)

    r = middleware_client.post("/api/sync-report", headers={"x-api-key": owner_key}, json={})
    assert r.status_code == 403
    assert r.json()["detail"] == "member_key_required"


def test_anonymous_401(middleware_client, db_session):
    r = middleware_client.post("/api/sync-report", json={})
    assert r.status_code == 401


# ── 3. caps: 201 loop_runs → 200 stored + truncated; detail >2000 truncated ─


def test_caps_loop_runs_and_detail_truncation(middleware_client, db_session):
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    _, member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)

    long_detail = "x" * 2500  # exceeds the 2000-char server-side cap per field
    # Only include the long detail in a few items to stay under 256KB body cap.
    # The rest use short detail. Total: 200 items * ~200 bytes + 1 * 2500 = ~42KB.
    items = []
    for i in range(201):
        item = {
            "loop_slug": f"loop-{i}",
            "instance_key": "agent/default",
            "outcome": "success",
            "detail": long_detail if i < 1 else "short",
        }
        items.append(item)
    payload = {"loop_runs": items}

    r = middleware_client.post("/api/sync-report", headers={"x-api-key": member_key}, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"]["loop_runs"] == 200
    assert body["truncated"]["loop_runs"] == 1

    from app.models import LoopRun

    all_runs = db_session.query(LoopRun).all()
    assert len(all_runs) == 200
    # Every detail field must be <= 2000 chars.
    for lr in all_runs:
        assert lr.detail is not None
        assert len(lr.detail) <= 2000


# ── 4. oversize body → 413 ────────────────────────────────────────────────────


def test_oversize_body_413(middleware_client, db_session):
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    _, member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)

    # Build a payload > 256 KB.
    big_detail = "A" * (300 * 1024)  # 300 KB in one field
    payload = {
        "loop_runs": [{"loop_slug": "big", "instance_key": "x", "outcome": "success", "detail": big_detail}]
    }

    r = middleware_client.post("/api/sync-report", headers={"x-api-key": member_key}, json=payload)
    assert r.status_code == 413
    assert r.json()["detail"] == "payload_too_large"


# ── 5. empty payload → 200, FleetMember.updated_at bumped ─────────────────────


def test_empty_payload_bumps_updated_at(middleware_client, db_session):
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    _, member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)

    from app.models import APIKey, FleetMember

    key_row = (
        db_session.query(APIKey)
        .filter(APIKey.key_hash == hashlib.sha256(member_key.encode()).hexdigest())
        .first()
    )
    member = db_session.query(FleetMember).filter(FleetMember.api_key_id == key_row.id).first()
    old_updated = member.updated_at

    r = middleware_client.post("/api/sync-report", headers={"x-api-key": member_key}, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"]["loop_runs"] == 0
    assert body["recorded"]["skill_errors"] == 0
    assert body["recorded"]["cron_health"] is False

    db_session.refresh(member)
    assert member.updated_at >= old_updated


# ── 6. rollup idempotency: run twice → same aggregate ─────────────────────────


def test_rollup_idempotent(middleware_client, db_session):
    from app.models import LoopRun
    from app.services.sync_report import rollup_loop_runs

    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    member_id_str, _ = _enroll_member(middleware_client, db_session, fleet, owner_key)
    member_id = uuid.UUID(member_id_str)

    now = datetime.now(UTC)
    for i in range(3):
        db_session.add(
            LoopRun(
                member_id=member_id,
                fleet_id=fleet.id,
                loop_slug="atomic-habits",
                instance_key="agent/default",
                outcome="success" if i < 2 else "failure",
                accepted_change=(i == 0),
                cost_usd=0.10 * (i + 1),
                duration_seconds=100 * (i + 1),
            )
        )
    db_session.commit()

    rollup_loop_runs(db_session, day=now.date())
    from app.models import LoopRunDailyRollup

    r1 = (
        db_session.query(LoopRunDailyRollup)
        .filter(
            LoopRunDailyRollup.member_id == member_id,
            LoopRunDailyRollup.loop_slug == "atomic-habits",
        )
        .first()
    )
    assert r1 is not None
    assert r1.runs == 3
    assert r1.successes == 2
    assert r1.failures == 1
    assert r1.accepted_changes == 1

    # Run again — should NOT double the counts.
    rollup_loop_runs(db_session, day=now.date())
    r2 = (
        db_session.query(LoopRunDailyRollup)
        .filter(
            LoopRunDailyRollup.member_id == member_id,
            LoopRunDailyRollup.loop_slug == "atomic-habits",
        )
        .all()
    )
    assert len(r2) == 1
    assert r2[0].runs == 3
    assert r2[0].accepted_changes == 1


# ── 7. pruner: 31-day-old deleted, 29-day kept, rollups untouched ───────────


def test_pruner_retention(middleware_client, db_session):
    from app.models import CronHealthSnapshot, LoopRun, LoopRunDailyRollup
    from app.services.sync_report import prune_raw

    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    member_id_str, _ = _enroll_member(middleware_client, db_session, fleet, owner_key)
    member_id = uuid.UUID(member_id_str)

    now = datetime.now(UTC)
    old = now - timedelta(days=31)
    recent = now - timedelta(days=29)

    db_session.add(
        LoopRun(
            member_id=member_id,
            fleet_id=fleet.id,
            loop_slug="x",
            instance_key="i",
            outcome="success",
            created_at=old,
        )
    )
    db_session.add(
        LoopRun(
            member_id=member_id,
            fleet_id=fleet.id,
            loop_slug="y",
            instance_key="i",
            outcome="success",
            created_at=recent,
        )
    )
    db_session.add(
        CronHealthSnapshot(
            member_id=member_id,
            fleet_id=fleet.id,
            failed=[],
            total_count=0,
            ok_count=0,
            error_count=0,
            created_at=old,
        )
    )
    db_session.add(
        CronHealthSnapshot(
            member_id=member_id,
            fleet_id=fleet.id,
            failed=[],
            total_count=0,
            ok_count=0,
            error_count=0,
            created_at=recent,
        )
    )

    # Add a rollup that must survive pruning.
    db_session.add(
        LoopRunDailyRollup(
            fleet_id=fleet.id,
            member_id=member_id,
            loop_slug="x",
            day=(old - timedelta(days=1)).date(),
            runs=99,
            successes=99,
            failures=0,
            accepted_changes=5,
        )
    )
    db_session.commit()

    result = prune_raw(db_session, older_than_days=30)
    assert result["loop_runs"] == 1
    assert result["cron_health_snapshots"] == 1

    remaining_lr = db_session.query(LoopRun).filter(LoopRun.member_id == member_id).all()
    assert len(remaining_lr) == 1
    assert remaining_lr[0].loop_slug == "y"

    remaining_ch = (
        db_session.query(CronHealthSnapshot).filter(CronHealthSnapshot.member_id == member_id).all()
    )
    assert len(remaining_ch) == 1

    remaining_rollups = db_session.query(LoopRunDailyRollup).all()
    assert len(remaining_rollups) == 1
    assert remaining_rollups[0].runs == 99


# ── 8. cost-per-accepted-change math ─────────────────────────────────────────


def test_cost_per_accepted_change(middleware_client, db_session):
    from app.models import LoopRunDailyRollup
    from app.services.sync_report import cost_per_accepted_change

    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    member_id_str, _ = _enroll_member(middleware_client, db_session, fleet, owner_key)
    member_id = uuid.UUID(member_id_str)

    # cost_usd_total=10.00, accepted_changes=4 → 2.50
    db_session.add(
        LoopRunDailyRollup(
            fleet_id=fleet.id,
            member_id=member_id,
            loop_slug="atomic-habits",
            day=datetime.now(UTC).date(),
            runs=10,
            successes=8,
            failures=2,
            accepted_changes=4,
            cost_usd_total=10.0,
            duration_seconds_total=500,
        )
    )
    db_session.commit()

    cpac = cost_per_accepted_change(db_session, fleet_id=fleet.id)
    assert cpac is not None
    assert abs(cpac - 2.50) < 0.001

    # Zero accepted changes → None
    cpac_zero = cost_per_accepted_change(db_session, fleet_id=fleet.id, loop_slug="nonexistent")
    assert cpac_zero is None


# ── 9. cron_health failures capped at 50 ─────────────────────────────────────


def test_cron_health_failed_capped_at_50(middleware_client, db_session):
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    fleet = _mk_fleet(db_session, owner)
    _, member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)

    failed_55 = [
        {"job_name": f"job-{i}", "last_status": "error", "consecutive_failures": i} for i in range(55)
    ]
    payload = {"cron_health": {"failed": failed_55, "counts": {"total": 60, "ok": 5, "error": 55}}}

    r = middleware_client.post("/api/sync-report", headers={"x-api-key": member_key}, json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recorded"]["cron_health"] is True
    assert body["truncated"]["cron_health_failed"] == 5

    from app.models import CronHealthSnapshot

    ch = db_session.query(CronHealthSnapshot).first()
    assert ch is not None
    assert len(ch.failed) == 50


# ── 10. version bump contract ────────────────────────────────────────────────


def test_version_bumped_to_0_8_0():
    from app.version import __version__

    assert __version__ == "0.8.0", f"Expected version 0.8.0, got {__version__}"


# ── 11. admin endpoints: rollup + prune (master-only) ────────────────────────


def test_admin_rollup_master_only(middleware_client, db_session):
    """Non-master key → 403 on admin rollup."""
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)

    r = middleware_client.post("/api/admin/loop-run-rollup", headers={"x-api-key": owner_key}, json={})
    assert r.status_code == 403


def test_admin_prune_master_only(middleware_client, db_session):
    """Non-master key → 403 on admin prune."""
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)

    r = middleware_client.post(
        "/api/admin/sync-report-prune", headers={"x-api-key": owner_key}, json={"days": 30}
    )
    assert r.status_code == 403
