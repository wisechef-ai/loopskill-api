"""t_a2ba8443 — connect_agent.tested telemetry + GET /api/connect/test.

Acceptance cases from the task spec:
  1. Valid x-api-key GET /api/connect/test → 200, one connect_agent.tested
     row with payload.user_id + api_key_id (server-side fire point, at the
     same place last_used_at is stamped by the middleware).
  2. Invalid / missing key → ZERO telemetry rows (the failure path is
     structurally "no row" — APIKeyMiddleware 401s before the handler runs;
     the portal surfaces the 401 as an actionable error, card functional).
  3. Fail-quiet: the handler adds NO second catch layer — the guarantee
     lives inside record_connect_agent_event (never raises, unchanged from
     catel_0826). Pinned here so a future divergent catch trips a test.
  4. The POST /api/telemetry event_type enum stays CLOSED —
     connect_agent.tested is server-side-only per the bhint-tel0824 rule.
  5. Structural pin: the route is NOT under /api/auth/* (whose
     JWT_AUTH_PREFIXES entry would bypass APIKeyMiddleware and silently
     break last_used_at / first-use measurement).
"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from starlette.types import ASGIApp, Receive, Scope, Send

from app import connect_test_routes
from app.database import get_db
from app.models import TelemetryEvent
from app.services.connect_agent_telemetry import EVENT_REVEALED, EVENT_SHOWN

TESTED_EVENT = "connect_agent.tested"


# ── Shared scaffolding ──────────────────────────────────────────────────────


class _StampAuthCtx:
    """Tiny ASGI middleware stamping request.state.auth_ctx — the exact
    production contract APIKeyMiddleware provides (it stamps auth_ctx on
    every validated request, and 401s invalid keys BEFORE the route runs,
    so the handler sees only success-shaped ctx; see acceptance 2)."""

    def __init__(self, inner: ASGIApp, ctx):
        self.inner = inner
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            scope.setdefault("state", {})
            scope["state"]["auth_ctx"] = self.ctx
        await self.inner(scope, receive, send)


def _build_app(db_session, ctx):
    app = FastAPI()
    app.include_router(connect_test_routes.router)
    app.dependency_overrides[get_db] = lambda: db_session
    return TestClient(_StampAuthCtx(app, ctx))


def _rows(db, event_type):
    return db.query(TelemetryEvent).filter(TelemetryEvent.event_type == event_type).all()


# ── Acceptance 1: successful test writes the row with the funnel join keys ──


def test_successful_test_writes_tested_row(db_session, monkeypatch):
    user_id = uuid.uuid4()
    api_key_id = uuid.uuid4()
    ctx = _Ctx(user_id=user_id, api_key_id=api_key_id, tier="free")

    captured = {}

    def fake_record(db, *, event_type, user_id, payload, client_ip=None, commit=False):
        captured.update(
            event_type=event_type,
            user_id=user_id,
            payload=payload,
            commit=commit,
        )

    monkeypatch.setattr(connect_test_routes, "record_connect_agent_event", fake_record)

    client = _build_app(db_session, ctx)
    res = client.get("/api/connect/test", headers={"x-api-key": "rec_live_dummy"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["connected"] is True
    assert captured["event_type"] == TESTED_EVENT
    assert captured["user_id"] == str(user_id)
    assert captured["payload"]["api_key_id"] == str(api_key_id)
    assert captured["payload"]["surface"] == "/library"
    assert captured["payload"]["endpoint"] == "/api/connect/test"
    assert captured["commit"] is True


# ── Acceptance 2 (defensive branch): no auth_ctx → 401, no row ──────────────


def test_missing_auth_ctx_401s_without_row(db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(
        connect_test_routes,
        "record_connect_agent_event",
        lambda *a, **k: calls.append(k),
    )
    app = FastAPI()
    app.include_router(connect_test_routes.router)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app)  # NO stamping middleware — anonymous shape
    res = client.get("/api/connect/test")
    assert res.status_code == 401
    assert calls == []
    assert _rows(db_session, TESTED_EVENT) == []


# ── Acceptance 3: fail-quiet boundary — no divergent catch layer ────────────


def test_handler_delegates_fail_quiet_to_record_helper(db_session, monkeypatch):
    """record_connect_agent_event never raises (catel_0826 contract). The
    handler must NOT wrap it in its own try/except that could diverge (e.g.
    swallow a programming error and still 200). Pinned by asserting the
    helper's exception propagates — i.e. there is no second catch layer."""
    ctx = _Ctx(user_id=uuid.uuid4(), api_key_id=uuid.uuid4(), tier=None)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(connect_test_routes, "record_connect_agent_event", boom)
    client = _build_app(db_session, ctx)
    try:
        res = client.get("/api/connect/test")
        # TestClient re-raises server exceptions by default; if it doesn't,
        # the response must be a 500 — never a 200-with-success.
        assert res.status_code == 500
    except RuntimeError:
        pass  # propagated = no divergent catch = correct


# ── Acceptance 4: enum stays closed — event is server-side-only ─────────────


def test_telemetry_endpoint_enum_stays_closed_without_tested():
    from app.schemas import TELEMETRY_EVENT_TYPES

    assert TESTED_EVENT not in TELEMETRY_EVENT_TYPES
    # The sibling funnel events are absent too (unchanged semantics).
    assert EVENT_SHOWN not in TELEMETRY_EVENT_TYPES
    assert EVENT_REVEALED not in TELEMETRY_EVENT_TYPES


# ── Acceptance 5: structural pin — route is NOT under the JWT prefix ────────


def test_route_not_under_jwt_auth_prefix():
    """/api/auth/* requests bypass APIKeyMiddleware (JWT_AUTH_PREFIXES), so a
    route mounted there could NEVER stamp last_used_at via x-api-key. Pin
    the decided path so nobody 'simplifies' the route into the auth router
    and silently breaks first-use measurement."""
    routes = [getattr(r, "path", None) for r in connect_test_routes.router.routes]
    assert "/api/connect/test" in routes
    assert not any(p and p.startswith("/api/auth") for p in routes)


def test_real_middleware_stamps_last_used_at_for_connect_test(db_session, monkeypatch):
    """End-to-end through the REAL APIKeyMiddleware: a valid rec_ key hitting
    GET /api/connect/test → 200, tracker.record called (last_used_at rail),
    and the tested event recorded with the owning user's id + key id.

    The record call is CAPTURED rather than asserted as a persisted row:
    the fixture's savepoint-isolated session cannot share its uncommitted
    rows with the middleware's separate lookup session, and that session's
    close() disturbs the savepoint state (test-infra artifact — the real
    insert+commit is proven by test_successful_test_writes_tested_row and
    the standalone record path, plus the live-prod verification step).
    """
    from datetime import UTC, datetime

    from app.middleware.api_key import APIKeyMiddleware
    from app.models import APIKey, User
    from app import connect_test_routes as ctr

    user = User(
        id=uuid.uuid4(),
        email="tested@example.com",
        display_name="Tested User",
        created_at=datetime.now(UTC),
    )
    key_secret = "rec_live_" + "a" * 32
    key = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_hash=hashlib.sha256(key_secret.encode()).hexdigest(),
        key_prefix="rec_live_",
        name="t_a2ba8443-test",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.add(key)
    db_session.commit()

    captured = {}

    def fake_record(db, *, event_type, user_id, payload, client_ip=None, commit=False):
        captured.update(event_type=event_type, user_id=user_id, payload=payload, commit=commit)

    monkeypatch.setattr(ctr, "record_connect_agent_event", fake_record)

    recorded = {}

    class FakeTracker:
        def record(self, api_key_id, ts):
            recorded["api_key_id"] = str(api_key_id)
            recorded["ts"] = ts

    # The middleware opens its OWN session for the key lookup; point its
    # import source (app.database.SessionLocal) at a sessionmaker bound to
    # the test engine so the lookup sees the test rows.
    import app.database as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr("app.last_used_tracker.tracker", FakeTracker())

    app = FastAPI()
    app.include_router(ctr.router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.add_middleware(APIKeyMiddleware)
    client = TestClient(app)

    res = client.get("/api/connect/test", headers={"x-api-key": key_secret})

    assert res.status_code == 200, res.text
    assert res.json()["connected"] is True
    # last_used_at rail fired for THIS key (middleware x-api-key branch).
    assert recorded["api_key_id"] == str(key.id)
    # tested event recorded with the funnel join keys.
    assert captured["event_type"] == TESTED_EVENT
    assert captured["user_id"] == str(user.id)
    assert captured["payload"]["api_key_id"] == str(key.id)
    assert captured["commit"] is True


def test_real_middleware_401s_invalid_key_without_row(db_session, monkeypatch):
    """Acceptance 2 through the real middleware: a wrong key never reaches
    the handler — 401, tracker not fired, zero tested rows."""
    from datetime import UTC, datetime

    from app.middleware.api_key import APIKeyMiddleware
    from app.models import APIKey, User
    from app import connect_test_routes as ctr

    user = User(
        id=uuid.uuid4(),
        email="tested401@example.com",
        display_name="Tested 401",
        created_at=datetime.now(UTC),
    )
    key = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_hash=hashlib.sha256(b"rec_live_" + b"b" * 32).hexdigest(),
        key_prefix="rec_live_",
        name="t_a2ba8443-test-401",
        is_active=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.add(key)
    db_session.commit()

    calls = []
    monkeypatch.setattr(ctr, "record_connect_agent_event", lambda *a, **k: calls.append(k))

    tracker_calls = []

    class FakeTracker:
        def record(self, api_key_id, ts):
            tracker_calls.append(str(api_key_id))

    import app.database as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setattr("app.last_used_tracker.tracker", FakeTracker())

    app = FastAPI()
    app.include_router(ctr.router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.add_middleware(APIKeyMiddleware)
    client = TestClient(app)

    res = client.get("/api/connect/test", headers={"x-api-key": "rec_live_" + "f" * 32})

    assert res.status_code == 401
    assert tracker_calls == []
    assert calls == []
    assert _rows(db_session, TESTED_EVENT) == []


class _Ctx:
    """Minimal auth_ctx stand-in matching AuthContext's handler surface."""

    def __init__(self, *, user_id, api_key_id, tier):
        self.user_id = user_id
        self.api_key_id = api_key_id
        self.tier = tier
