"""Tests for telemetry endpoint validation — D3 Sprint 4.

Covers all validation rules from the contract:
1.  bad event_type → 422
2.  duration_seconds > 86400 → 422
3.  duration_seconds < 0 → 422
4.  agent_class_hash too short (< 8 chars) → 422
5.  agent_class_hash with uppercase letters → 422
6.  agent_class_hash with non-hex chars → 422
7.  unknown skill_slug → 404 {detail: 'unknown skill_slug'}
8.  retry_count < 0 → 422
9.  agent_class_hash at min length (8 chars) → 201 (boundary valid)
10. agent_class_hash at max length (64 chars) → 201 (boundary valid)
11. duration_seconds = 86400 → 201 (boundary valid)
12. goal_class unknown value → 201 (open enum, stored as-is)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import make_skill


def _post(client: TestClient, body: dict):
    return client.post("/api/telemetry", json=body)


class TestTelemetryValidation:
    """Validation rejection and boundary tests."""

    # ── event_type ────────────────────────────────────────────────────

    def test_bad_event_type_rejected_422(self, client: TestClient, db_session: Session):
        """Unknown event_type returns 422 Unprocessable Entity."""
        resp = _post(client, {
            "event_type": "hacked",
            "skill_slug": None,
        })
        assert resp.status_code == 422, resp.text

    def test_event_type_empty_string_rejected(self, client: TestClient, db_session: Session):
        """Empty event_type is not in allowed set → 422."""
        resp = _post(client, {"event_type": ""})
        assert resp.status_code == 422

    # ── duration_seconds ─────────────────────────────────────────────

    def test_duration_over_86400_rejected_422(self, client: TestClient, db_session: Session):
        """duration_seconds > 86400 returns 422."""
        resp = _post(client, {
            "event_type": "task_completed",
            "duration_seconds": 86401,
        })
        assert resp.status_code == 422, resp.text

    def test_duration_negative_rejected_422(self, client: TestClient, db_session: Session):
        """Negative duration_seconds returns 422."""
        resp = _post(client, {
            "event_type": "task_completed",
            "duration_seconds": -1,
        })
        assert resp.status_code == 422

    def test_duration_86400_accepted(self, client: TestClient, db_session: Session):
        """duration_seconds=86400 is the upper boundary — must be accepted."""
        resp = _post(client, {
            "event_type": "task_completed",
            "duration_seconds": 86400,
        })
        assert resp.status_code == 201

    # ── agent_class_hash ─────────────────────────────────────────────

    def test_agent_hash_too_short_rejected(self, client: TestClient, db_session: Session):
        """agent_class_hash shorter than 8 hex chars → 422."""
        resp = _post(client, {
            "event_type": "task_completed",
            "agent_class_hash": "abc1234",  # 7 chars
        })
        assert resp.status_code == 422

    def test_agent_hash_uppercase_rejected(self, client: TestClient, db_session: Session):
        """agent_class_hash with uppercase hex → 422 (regex requires lowercase)."""
        resp = _post(client, {
            "event_type": "task_completed",
            "agent_class_hash": "ABCDEF12",  # uppercase
        })
        assert resp.status_code == 422

    def test_agent_hash_non_hex_rejected(self, client: TestClient, db_session: Session):
        """agent_class_hash with non-hex chars → 422."""
        resp = _post(client, {
            "event_type": "task_completed",
            "agent_class_hash": "xyz12345",  # 'x', 'y', 'z' not hex
        })
        assert resp.status_code == 422

    def test_agent_hash_min_length_accepted(self, client: TestClient, db_session: Session):
        """agent_class_hash = 8 hex chars is the minimum — must be accepted."""
        resp = _post(client, {
            "event_type": "task_completed",
            "agent_class_hash": "abcdef01",  # exactly 8
        })
        assert resp.status_code == 201

    def test_agent_hash_max_length_accepted(self, client: TestClient, db_session: Session):
        """agent_class_hash = 64 hex chars is the maximum — must be accepted."""
        resp = _post(client, {
            "event_type": "task_completed",
            "agent_class_hash": "a" * 64,  # exactly 64
        })
        assert resp.status_code == 201

    def test_agent_hash_over_max_rejected(self, client: TestClient, db_session: Session):
        """agent_class_hash longer than 64 chars → 422."""
        resp = _post(client, {
            "event_type": "task_completed",
            "agent_class_hash": "a" * 65,
        })
        assert resp.status_code == 422

    # ── skill_slug resolution ─────────────────────────────────────────

    def test_unknown_skill_slug_returns_404(self, client: TestClient, db_session: Session):
        """Providing a skill_slug not in skills table → 404."""
        resp = _post(client, {
            "event_type": "install",
            "skill_slug": "does-not-exist",
        })
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == "unknown skill_slug"

    def test_known_skill_slug_accepted(self, client: TestClient, db_session: Session):
        """A skill_slug that exists in skills table → 201."""
        make_skill(db_session, slug="real-skill")
        resp = _post(client, {
            "event_type": "install",
            "skill_slug": "real-skill",
        })
        assert resp.status_code == 201

    # ── retry_count ─────────────────────────────────────────────────

    def test_retry_count_negative_rejected(self, client: TestClient, db_session: Session):
        """Negative retry_count → 422."""
        resp = _post(client, {
            "event_type": "task_failed",
            "retry_count": -1,
        })
        assert resp.status_code == 422

    def test_retry_count_zero_accepted(self, client: TestClient, db_session: Session):
        """retry_count=0 is valid."""
        resp = _post(client, {
            "event_type": "task_completed",
            "retry_count": 0,
        })
        assert resp.status_code == 201

    # ── goal_class (open enum) ───────────────────────────────────────

    def test_unknown_goal_class_accepted_stored_as_is(self, client: TestClient, db_session: Session):
        """goal_class is an open enum — unknown values stored without rejection."""
        from app.models import TelemetryEvent
        from uuid import UUID

        resp = _post(client, {
            "event_type": "task_completed",
            "goal_class": "totally-custom-goal",
        })
        assert resp.status_code == 201
        ev = db_session.get(TelemetryEvent, UUID(resp.json()["event_id"]))
        assert ev.goal_class == "totally-custom-goal"

    def test_goal_class_65_chars_rejected_422(self, client: TestClient, db_session: Session):
        """F7: goal_class longer than 64 chars must be rejected with 422 (not 500).

        Migration column is VARCHAR(64). Without Pydantic validation, a 65-char value
        would hit Postgres and raise DataError → 500. Pydantic max_length=64 stops it.
        """
        too_long = "x" * 65
        resp = _post(client, {
            "event_type": "task_completed",
            "goal_class": too_long,
        })
        assert resp.status_code == 422, (
            f"Expected 422 for 65-char goal_class, got {resp.status_code} — F7 regression"
        )

    def test_goal_class_64_chars_accepted(self, client: TestClient, db_session: Session):
        """F7: goal_class exactly 64 chars is at the limit and must be accepted."""
        at_limit = "g" * 64
        resp = _post(client, {
            "event_type": "task_completed",
            "goal_class": at_limit,
        })
        assert resp.status_code == 201, (
            f"Expected 201 for 64-char goal_class, got {resp.status_code}"
        )

    # ── skill_slug validation (F8) ───────────────────────────────────

    def test_empty_skill_slug_rejected_422(self, client: TestClient, db_session: Session):
        """F8: empty string skill_slug → 422 (not routed to 404-on-unknown logic)."""
        resp = _post(client, {
            "event_type": "install",
            "skill_slug": "",
        })
        assert resp.status_code == 422, (
            f"Expected 422 for empty skill_slug, got {resp.status_code} — F8 regression"
        )

    def test_whitespace_only_skill_slug_rejected_422(self, client: TestClient, db_session: Session):
        """F8: whitespace-only skill_slug → 422 after strip_whitespace."""
        resp = _post(client, {
            "event_type": "install",
            "skill_slug": "   ",
        })
        assert resp.status_code == 422, (
            f"Expected 422 for whitespace skill_slug, got {resp.status_code} — F8 regression"
        )
