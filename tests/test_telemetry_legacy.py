"""Tests for legacy telemetry mode — D3 Sprint 4.

Ensures backward compatibility: callers that send only {event_type, skill_slug, payload}
continue to work exactly as before. The payload dict is JSON-serialised into the
payload TEXT column, and the typed columns are NULL.

Covers:
1. Legacy payload → payload column populated
2. Typed columns NULL when only legacy payload provided
3. skill_slug=None (anonymous telemetry) accepted
4. payload=None accepted (neither mode required)
5. Existing event_type values all accepted
6. Response shape unchanged: {status, event_id}
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import TelemetryEvent
from tests.conftest import make_skill


def _post(client: TestClient, body: dict):
    return client.post("/api/telemetry", json=body)


class TestLegacyTelemetry:
    """Legacy payload mode must continue to work unchanged."""

    def test_legacy_payload_stored_as_json(self, client: TestClient, db_session: Session):
        """Legacy payload dict is JSON-serialised into payload column."""
        make_skill(db_session, slug="legacy-skill")

        resp = _post(client, {
            "event_type": "task_completed",
            "skill_slug": "legacy-skill",
            "payload": {"freeform": "data", "version": "1.0.0"},
        })

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "recorded"
        assert "event_id" in body

        from uuid import UUID
        ev = db_session.get(TelemetryEvent, UUID(body["event_id"]))
        assert ev is not None
        stored = json.loads(ev.payload)
        assert stored == {"freeform": "data", "version": "1.0.0"}

    def test_legacy_typed_columns_null(self, client: TestClient, db_session: Session):
        """When only legacy payload sent, typed columns must be NULL."""
        make_skill(db_session, slug="legacy-null-skill")

        resp = _post(client, {
            "event_type": "install",
            "skill_slug": "legacy-null-skill",
            "payload": {"key": "value"},
        })
        assert resp.status_code == 201

        from uuid import UUID
        ev = db_session.get(TelemetryEvent, UUID(resp.json()["event_id"]))
        assert ev.goal_class is None
        assert ev.duration_seconds is None
        assert ev.retry_count is None
        assert ev.user_intervention is None
        assert ev.agent_class_hash is None

    def test_anonymous_telemetry_no_skill_slug(self, client: TestClient, db_session: Session):
        """skill_slug is optional; anonymous telemetry must be accepted."""
        resp = _post(client, {
            "event_type": "first_use",
            # no skill_slug
            "payload": {"anonymous": True},
        })
        assert resp.status_code == 201

        from uuid import UUID
        ev = db_session.get(TelemetryEvent, UUID(resp.json()["event_id"]))
        assert ev.skill_slug is None
        assert ev.skill_id is None

    def test_no_payload_no_typed_fields(self, client: TestClient, db_session: Session):
        """Bare minimum request (event_type only) → 201, all optional cols NULL."""
        resp = _post(client, {"event_type": "install"})
        assert resp.status_code == 201

        from uuid import UUID
        ev = db_session.get(TelemetryEvent, UUID(resp.json()["event_id"]))
        assert ev.payload is None
        assert ev.goal_class is None

    def test_all_valid_event_types_accepted(self, client: TestClient, db_session: Session):
        """Every allowed event_type must return 201."""
        for et in ("install", "first_use", "task_completed", "task_failed", "replaced"):
            resp = _post(client, {"event_type": et})
            assert resp.status_code == 201, f"Failed for event_type={et}: {resp.text}"

    def test_legacy_response_shape(self, client: TestClient, db_session: Session):
        """Response JSON shape is {status: 'recorded', event_id: <uuid-str>}."""
        make_skill(db_session, slug="shape-skill")

        resp = _post(client, {
            "event_type": "task_completed",
            "skill_slug": "shape-skill",
            "payload": {"status": "ok"},
        })
        assert resp.status_code == 201
        body = resp.json()
        assert set(body.keys()) >= {"status", "event_id"}
        assert body["status"] == "recorded"
        # event_id should be a valid UUID4 string
        from uuid import UUID
        UUID(body["event_id"])  # raises if invalid

    def test_empty_dict_payload_stored_as_json_not_null(self, client: TestClient, db_session: Session):
        """F9: payload={} must be stored as '{}' text, NOT NULL.

        Previously `if body.payload:` treated {} as falsy → NULL stored.
        """
        from uuid import UUID

        resp = _post(client, {
            "event_type": "install",
            "payload": {},
        })
        assert resp.status_code == 201
        ev = db_session.get(TelemetryEvent, UUID(resp.json()["event_id"]))
        assert ev.payload is not None, "payload={} must not be stored as NULL — F9 regression"
        assert ev.payload == "{}", f"Expected '{{}}', got {ev.payload!r} — F9 regression"
