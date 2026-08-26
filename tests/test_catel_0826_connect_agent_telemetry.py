"""catel_0826 (t_76db433e) — connect-agent card activation telemetry.

Acceptance cases from the task spec:
  1. Authed hit on GET /api/auth/first-key-reveal → connect_agent.shown row
     written with payload.user_id (server-side fire point — see
     app/services/connect_agent_telemetry.py for the documented deviation
     from the task's client-side decree).
  2. Successful one-time reveal → first_key.revealed row in the SAME
     response; generic-copy / consumed / no-cookie branch writes shown ONLY.
  3. No auth session → 401, zero telemetry rows (card never renders).
  4. Fail-quiet: a telemetry DB failure never breaks the reveal response
     contract (200-with-key on hit, 404 on miss).
  5. The POST /api/telemetry event_type enum stays CLOSED — the new names
     are server-side-only per the bhint-tel0824 rule (pinned so a future
     widening trips a loud test, not a silent forging surface).
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth_routes, first_key_routes
from app.database import get_db
from app.models import TelemetryEvent
from app.services.connect_agent_telemetry import EVENT_REVEALED, EVENT_SHOWN


# ── Shared scaffolding (mirrors test_flywheel_f12_first_key.py) ────────────


def _build_app():
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(first_key_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    return app


def _make_user():
    owner_id = uuid.uuid4()
    return SimpleNamespace(id=owner_id, display_name="Telemetry User")


def _rows(db, event_type):
    return db.query(TelemetryEvent).filter(TelemetryEvent.event_type == event_type).all()


# ── Acceptance 1+2: shown + revealed on the successful reveal path ─────────


def test_successful_reveal_writes_shown_and_revealed(db_session, monkeypatch):
    user = _make_user()

    monkeypatch.setattr(auth_routes, "get_current_user_optional", lambda request, db: user)

    def _consume(token, uid):
        return {
            "user_id": str(user.id),
            "key": "rec_live_tel0826",
            "prefix": "rec_live_t",
            "label": "first-key (auto)",
        }

    monkeypatch.setattr("app.services.first_key_reveal.consume_reveal", _consume)

    app = _build_app()
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        client.cookies.set(auth_routes.REVEAL_COOKIE_NAME, "reveal-tok")
        r = client.get("/api/auth/first-key-reveal")

    assert r.status_code == 200, r.text
    assert r.json()["key"] == "rec_live_tel0826"

    shown = _rows(db_session, EVENT_SHOWN)
    assert len(shown) == 1, "exactly one connect_agent.shown row per authed card render"
    payload = json.loads(shown[0].payload or "{}")
    assert payload["user_id"] == str(user.id), "funnel join key rides in the JSON payload"
    assert payload["surface"] == "/library"

    revealed = _rows(db_session, EVENT_REVEALED)
    assert len(revealed) == 1, "exactly one first_key.revealed row on the successful branch"
    rpayload = json.loads(revealed[0].payload or "{}")
    assert rpayload["user_id"] == str(user.id)
    assert rpayload["prefix"] == "rec_live_t"
    # The plaintext key itself must NEVER land in telemetry.
    assert "rec_live_tel0826" not in (revealed[0].payload or "")
    assert "rec_live_tel0826" not in (shown[0].payload or "")


def test_consumed_reveal_writes_shown_only(db_session, monkeypatch):
    """Returning session (reveal consumed/expired) → generic-copy card
    variant: shown counted, revealed NOT (spec: successful branch only)."""
    user = _make_user()
    monkeypatch.setattr(auth_routes, "get_current_user_optional", lambda request, db: user)
    monkeypatch.setattr("app.services.first_key_reveal.consume_reveal", lambda token, uid: None)

    app = _build_app()
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        client.cookies.set(auth_routes.REVEAL_COOKIE_NAME, "stale-tok")
        r = client.get("/api/auth/first-key-reveal")

    assert r.status_code == 404
    assert len(_rows(db_session, EVENT_SHOWN)) == 1
    assert len(_rows(db_session, EVENT_REVEALED)) == 0


def test_no_reveal_cookie_writes_shown_only(db_session, monkeypatch):
    """Authed member, no reveal cookie (generic-copy variant) → shown only."""
    user = _make_user()
    monkeypatch.setattr(auth_routes, "get_current_user_optional", lambda request, db: user)

    app = _build_app()
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        r = client.get("/api/auth/first-key-reveal")

    assert r.status_code == 404
    assert len(_rows(db_session, EVENT_SHOWN)) == 1
    assert len(_rows(db_session, EVENT_REVEALED)) == 0


# ── Acceptance 3: no session → 401, zero rows ──────────────────────────────


def test_anonymous_hit_writes_nothing(db_session, monkeypatch):
    monkeypatch.setattr(auth_routes, "get_current_user_optional", lambda request, db: None)

    app = _build_app()
    app.dependency_overrides[get_db] = lambda: db_session
    with TestClient(app) as client:
        r = client.get("/api/auth/first-key-reveal")

    assert r.status_code == 401
    assert len(_rows(db_session, EVENT_SHOWN)) == 0
    assert len(_rows(db_session, EVENT_REVEALED)) == 0


# ── Acceptance 4: fail-quiet — telemetry failure never breaks the contract ─


def test_telemetry_db_failure_still_returns_the_key(db_session, monkeypatch):
    """A db.add/commit failure inside the telemetry helper must not fail
    the reveal response (200-with-key on hit — the fail-quiet contract)."""
    user = _make_user()
    monkeypatch.setattr(auth_routes, "get_current_user_optional", lambda request, db: user)

    def _consume(token, uid):
        return {
            "user_id": str(user.id),
            "key": "rec_live_fq",
            "prefix": "rec_live_f",
            "label": "first-key (auto)",
        }

    monkeypatch.setattr("app.services.first_key_reveal.consume_reveal", _consume)

    class _BrokenSession:
        def add(self, *_a, **_k):
            raise RuntimeError("telemetry store down")

        def rollback(self):
            pass

    app = _build_app()
    app.dependency_overrides[get_db] = lambda: _BrokenSession()
    with TestClient(app) as client:
        client.cookies.set(auth_routes.REVEAL_COOKIE_NAME, "reveal-tok")
        r = client.get("/api/auth/first-key-reveal")

    assert r.status_code == 200, "fail-quiet: reveal contract survives telemetry outage"
    assert r.json()["key"] == "rec_live_fq"


# ── Acceptance 5: the HTTP enum stays closed (pin) ─────────────────────────


def test_telemetry_endpoint_enum_stays_closed():
    """The two new names are SERVER-SIDE-ONLY. If someone widens
    TELEMETRY_EVENT_TYPES to accept them over POST /api/telemetry, that
    re-opens a public forging surface for the funnel — this pin trips
    loudly instead (rule recorded in bundle_hint_telemetry.py + here)."""
    from app.schemas import TELEMETRY_EVENT_TYPES

    assert EVENT_SHOWN not in TELEMETRY_EVENT_TYPES
    assert EVENT_REVEALED not in TELEMETRY_EVENT_TYPES


def test_event_names_match_task_spec():
    """Names are decided (do not re-litigate): exact strings from the task."""
    assert EVENT_SHOWN == "connect_agent.shown"
    assert EVENT_REVEALED == "first_key.revealed"
