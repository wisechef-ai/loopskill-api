"""feat/member-loop-apply — tests for the placement chain's last mile.

Server half: GET /api/my/loop-assignments (member-key pull surface).
Client half: app.loop_apply.apply_assignments (manifests → Hermes cron jobs).

Fixture pattern mirrors tests/test_activate0701_sync_report.py (real User /
APIKey / Fleet rows through the middleware app factory).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

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
        display_name="loop-apply-owner",
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
        name="loop-apply-fleet",
        fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
    )
    db.add(fleet)
    db.flush()
    return fleet


def _enroll_member(client, db, fleet, owner_key, *, host="agent-host", profile="default"):
    r = client.post(
        f"/api/fleets/{fleet.id}/members",
        headers={"x-api-key": owner_key},
        json={"host": host, "profile": profile, "skills_dir": "~/.hermes/loopskill"},
    )
    assert r.status_code == 201, r.text
    return r.json()["member_id"], r.json()["api_key"]


def _mk_manifest(db, owner, *, loop_id="daily-brief", schedule="0 7 * * *", prompt="Do the daily brief."):
    from app.models import LoopManifest

    m = LoopManifest(
        id=uuid.uuid4(),
        loop_id=loop_id,
        owner_user_id=owner.id,
        schedule=schedule,
        prompt=prompt,
        skills=[{"id": "discord-post-from-cron", "hash": "sha256:abc"}],
        requires={},
        secret_refs=[],
    )
    db.add(m)
    db.flush()
    return m


def _mk_placement(db, fleet, member_id, *, loop_key="daily-brief", status="assigned", epoch=1):
    from app.models import LoopPlacement

    p = LoopPlacement(
        id=uuid.uuid4(),
        fleet_id=fleet.id,
        loop_key=loop_key,
        member_id=uuid.UUID(str(member_id)),
        status=status,
        placement_epoch=epoch,
    )
    db.add(p)
    db.flush()
    return p


# ═══════════════ Server half: GET /api/my/loop-assignments ═══════════════


class TestAssignmentsAuth:
    def test_anonymous_401(self, middleware_client):
        r = middleware_client.get("/api/my/loop-assignments")
        assert r.status_code == 401

    def test_non_member_key_403(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        r = middleware_client.get("/api/my/loop-assignments", headers={"x-api-key": owner_key})
        assert r.status_code == 403


class TestAssignmentsRead:
    def test_member_sees_assignment_with_manifest(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_id, member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)
        _mk_manifest(db_session, owner, loop_id="daily-brief")
        _mk_placement(db_session, fleet, member_id, loop_key="daily-brief", epoch=2)

        r = middleware_client.get("/api/my/loop-assignments", headers={"x-api-key": member_key})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 1
        a = body["assignments"][0]
        assert a["loop_key"] == "daily-brief"
        assert a["epoch"] == 2
        assert a["manifest"]["schedule"] == "0 7 * * *"
        assert a["manifest"]["prompt"] == "Do the daily brief."
        assert a["manifest"]["skills"][0]["id"] == "discord-post-from-cron"

    def test_placement_without_manifest_returns_null(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_id, member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)
        _mk_placement(db_session, fleet, member_id, loop_key="ghost-loop")

        r = middleware_client.get("/api/my/loop-assignments", headers={"x-api-key": member_key})
        assert r.status_code == 200
        assert r.json()["assignments"][0]["manifest"] is None

    def test_draining_and_removed_excluded(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_id, member_key = _enroll_member(middleware_client, db_session, fleet, owner_key)
        _mk_placement(db_session, fleet, member_id, loop_key="draining-loop", status="draining")
        _mk_placement(db_session, fleet, member_id, loop_key="removed-loop", status="removed")
        _mk_placement(db_session, fleet, member_id, loop_key="active-loop", status="active")

        r = middleware_client.get("/api/my/loop-assignments", headers={"x-api-key": member_key})
        assert r.status_code == 200
        keys = [a["loop_key"] for a in r.json()["assignments"]]
        assert keys == ["active-loop"]

    def test_other_members_placements_invisible(self, middleware_client, db_session):
        owner = _mk_user(db_session)
        owner_key = _mk_key(db_session, owner)
        fleet = _mk_fleet(db_session, owner)
        member_a, _key_a = _enroll_member(middleware_client, db_session, fleet, owner_key, host="host-a")
        _member_b, key_b = _enroll_member(middleware_client, db_session, fleet, owner_key, host="host-b")
        _mk_placement(db_session, fleet, member_a, loop_key="a-only-loop")

        r = middleware_client.get("/api/my/loop-assignments", headers={"x-api-key": key_b})
        assert r.status_code == 200
        assert r.json()["count"] == 0


# ═══════════════ Client half: apply_assignments ═══════════════


def _assignment(loop_key="daily-brief", *, epoch=1, manifest="default"):
    if manifest == "default":
        manifest = {
            "loop_id": loop_key,
            "schedule": "0 7 * * *",
            "prompt": "Do the daily brief.",
            "skills": [{"id": "discord-post-from-cron", "hash": "sha256:abc"}],
            "enabled": True,
            "deliver": "origin",
            "model": None,
        }
    return {
        "loop_key": loop_key,
        "placement_id": uuid.uuid4().hex,
        "epoch": epoch,
        "status": "assigned",
        "manifest": manifest,
    }


class TestApplyAssignments:
    def test_create_managed_job(self, tmp_path: Path):
        from app.loop_apply import apply_assignments

        jobs = tmp_path / "jobs.json"
        result = apply_assignments([_assignment()], jobs)
        assert result.created == ["daily-brief"]
        data = json.loads(jobs.read_text())
        job = data["jobs"][0]
        assert job["name"] == "loopskill/daily-brief"
        assert job["schedule"]["expr"] == "0 7 * * *"
        assert job["skills"] == ["discord-post-from-cron"]
        assert job["tags"] == ["tier1", "loopskill-managed", "daily-brief"]
        assert job["loopskill"]["epoch"] == 1

    def test_reapply_unchanged_is_noop(self, tmp_path: Path):
        from app.loop_apply import apply_assignments

        jobs = tmp_path / "jobs.json"
        apply_assignments([_assignment()], jobs)
        before = jobs.read_text()
        result = apply_assignments([_assignment()], jobs)
        assert not result.changed
        assert jobs.read_text() == before

    def test_update_on_manifest_change_preserves_run_state(self, tmp_path: Path):
        from app.loop_apply import apply_assignments

        jobs = tmp_path / "jobs.json"
        apply_assignments([_assignment()], jobs)
        # Simulate the scheduler having run the job.
        data = json.loads(jobs.read_text())
        data["jobs"][0]["last_run_at"] = "2026-07-17T07:00:00+00:00"
        data["jobs"][0]["last_status"] = "ok"
        original_id = data["jobs"][0]["id"]
        jobs.write_text(json.dumps(data))

        changed = _assignment(epoch=2)
        changed["manifest"]["schedule"] = "0 9 * * *"
        result = apply_assignments([changed], jobs)
        assert result.updated == ["daily-brief"]
        job = json.loads(jobs.read_text())["jobs"][0]
        assert job["schedule"]["expr"] == "0 9 * * *"
        assert job["last_run_at"] == "2026-07-17T07:00:00+00:00"  # run-state preserved
        assert job["id"] == original_id  # identity stable across updates
        assert job["loopskill"]["epoch"] == 2

    def test_undeploy_removes_managed_only(self, tmp_path: Path):
        from app.loop_apply import apply_assignments

        jobs = tmp_path / "jobs.json"
        # A user-owned job that must NEVER be touched.
        user_job = {
            "id": "abc123",
            "name": "my-own-cron",
            "prompt": "mine",
            "schedule": {"kind": "cron", "expr": "0 5 * * *"},
        }
        jobs.write_text(json.dumps({"jobs": [user_job]}))

        apply_assignments([_assignment()], jobs)
        assert len(json.loads(jobs.read_text())["jobs"]) == 2

        # Assignment set now empty → managed job removed, user job untouched.
        result = apply_assignments([], jobs)
        assert result.removed == ["daily-brief"]
        remaining = json.loads(jobs.read_text())["jobs"]
        assert [j["name"] for j in remaining] == ["my-own-cron"]

    def test_stale_epoch_skipped(self, tmp_path: Path):
        from app.loop_apply import apply_assignments

        jobs = tmp_path / "jobs.json"
        apply_assignments([_assignment(epoch=5)], jobs)
        stale = _assignment(epoch=3)
        stale["manifest"]["prompt"] = "STALE PROMPT — must not land."
        result = apply_assignments([stale], jobs)
        assert result.skipped == [{"loop_key": "daily-brief", "reason": "stale_epoch"}]
        job = json.loads(jobs.read_text())["jobs"][0]
        assert job["prompt"] == "Do the daily brief."
        assert job["loopskill"]["epoch"] == 5

    def test_null_manifest_and_invalid_schedule_skipped(self, tmp_path: Path):
        from app.loop_apply import apply_assignments

        jobs = tmp_path / "jobs.json"
        bad_schedule = _assignment("bad-sched")
        bad_schedule["manifest"]["schedule"] = "not a schedule"
        no_manifest = _assignment("ghost", manifest=None)
        result = apply_assignments([bad_schedule, no_manifest], jobs)
        assert not result.created
        reasons = {s["loop_key"]: s["reason"] for s in result.skipped}
        assert reasons == {"bad-sched": "invalid_schedule", "ghost": "no_manifest"}
        assert not jobs.exists()  # nothing to write

    def test_shorthand_schedule_accepted(self, tmp_path: Path):
        from app.loop_apply import apply_assignments

        jobs = tmp_path / "jobs.json"
        a = _assignment("half-hourly")
        a["manifest"]["schedule"] = "every 30m"
        result = apply_assignments([a], jobs)
        assert result.created == ["half-hourly"]
