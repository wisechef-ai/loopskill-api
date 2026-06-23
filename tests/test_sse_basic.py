"""Single-client SSE flow — v7 Phase D.

httpx 0.28's ``ASGITransport`` buffers the entire response before returning
(``response_complete.wait()``), so ``client.stream(...)`` is unusable for an
unbounded SSE endpoint — it would hang forever.  These tests therefore drive
the ASGI app directly: a custom ``asgi_sse_request()`` helper opens the
request, hands back the receive/send queues, and lets the test pull body
chunks one at a time.
"""
from __future__ import annotations

import asyncio
import json
from typing import Generator
from uuid import uuid4

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Cookbook, User
from app.sync_fanout import publish_event, reset_fanout


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture) -> Generator[Session, None, None]:
    SessionLocal = sessionmaker(bind=engine_fixture, autocommit=False, autoflush=False)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _make_user_and_cookbook(db: Session, *, tier: str = "cook") -> tuple[User, Cookbook]:
    user = User(
        id=uuid4(),
        display_name="SSE Tester",
        email=f"{uuid4()}@test.example",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(user)
    db.flush()
    cb = Cookbook(id=uuid4(), name="SSE Cookbook", bundle_owner=user.id)
    db.add(cb)
    db.commit()
    return user, cb


class _InjectAuthASGI:
    """Pure-ASGI middleware that stamps ``request.state.api_key_user_id``.

    Avoids :class:`starlette.middleware.base.BaseHTTPMiddleware`, which
    buffers streaming responses and breaks SSE.
    """

    def __init__(self, app, uid):
        self.app = app
        self.uid = uid

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from starlette.datastructures import State
            state = scope.get("state")
            if state is None:
                state = State()
                scope["state"] = state
            state.api_key_user_id = self.uid
            state.api_key_id = None
        await self.app(scope, receive, send)


def _build_app(db: Session, *, api_key_user_id) -> FastAPI:
    from app.cookbook_routes import router as cookbook_router
    from app.sse_routes import router as sse_router

    app = FastAPI()

    def _override_get_db():
        SessionLocal = sessionmaker(bind=db.bind, autocommit=False, autoflush=False)
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    app.add_middleware(_InjectAuthASGI, uid=api_key_user_id)
    app.include_router(cookbook_router)
    app.include_router(sse_router)
    return app


class SSEDriver:
    """Drives a single ASGI request directly so streaming responses work."""

    def __init__(self, app, path: str, headers: list[tuple[bytes, bytes]] | None = None):
        self.app = app
        self.path = path
        self.headers = headers or []
        self._send_q: asyncio.Queue = asyncio.Queue()
        self._recv_q: asyncio.Queue = asyncio.Queue()
        self._app_task: asyncio.Task | None = None
        self._buffer = b""
        self.status: int | None = None
        self.response_headers: list[tuple[bytes, bytes]] = []

    async def __aenter__(self):
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("127.0.0.1", 12345),
            "root_path": "",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": b"",
            "headers": self.headers,
        }
        await self._recv_q.put({"type": "http.request", "body": b"", "more_body": False})

        async def receive():
            return await self._recv_q.get()

        async def send(message):
            await self._send_q.put(message)

        self._app_task = asyncio.create_task(self.app(scope, receive, send))
        # Pump messages until we've got http.response.start.
        msg = await asyncio.wait_for(self._send_q.get(), timeout=2.0)
        assert msg["type"] == "http.response.start", msg
        self.status = msg["status"]
        self.response_headers = msg.get("headers", [])
        return self

    async def __aexit__(self, *exc):
        await self._recv_q.put({"type": "http.disconnect"})
        if self._app_task and not self._app_task.done():
            try:
                await asyncio.wait_for(self._app_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._app_task.cancel()

    async def read_chunk(self, *, timeout: float = 2.0) -> bytes:
        msg = await asyncio.wait_for(self._send_q.get(), timeout=timeout)
        assert msg["type"] == "http.response.body", msg
        return msg.get("body", b"")

    async def read_lines(self, n: int, *, timeout: float = 2.0) -> list[str]:
        out: list[str] = []
        while len(out) < n:
            if b"\n" not in self._buffer:
                self._buffer += await self.read_chunk(timeout=timeout)
                continue
            line, self._buffer = self._buffer.split(b"\n", 1)
            out.append(line.decode())
        return out


# ── Tests ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sse_initial_heartbeat_then_event(db_session):
    reset_fanout()
    user, cb = _make_user_and_cookbook(db_session)
    app = _build_app(db_session, api_key_user_id=user.id)

    async with SSEDriver(app, f"/api/cookbooks/{cb.id}/sync/sse") as drv:
        assert drv.status == 200
        ct = dict(drv.response_headers).get(b"content-type", b"").decode()
        assert ct.startswith("text/event-stream")

        first = await drv.read_lines(3)
        assert first[0] == "event: ping"
        assert first[1] == "data: {}"
        assert first[2] == ""

        # Now publish from the test loop.
        eid = await publish_event(
            str(cb.id),
            {"slug": "alpha", "version": "1.0.0", "action": "version_published"},
        )

        ev = await drv.read_lines(4)
        assert ev[0] == f"id: {eid}"
        assert ev[1] == "event: cookbook_event"
        assert ev[2].startswith("data: ")
        payload = json.loads(ev[2][len("data: "):])
        assert payload["slug"] == "alpha"
        assert payload["version"] == "1.0.0"
        assert ev[3] == ""


@pytest.mark.asyncio
async def test_sse_anon_blocked(db_session):
    """Free / anon callers can't open an SSE stream."""
    reset_fanout()
    user, cb = _make_user_and_cookbook(db_session, tier="free")
    app = _build_app(db_session, api_key_user_id=user.id)

    async with SSEDriver(app, f"/api/cookbooks/{cb.id}/sync/sse") as drv:
        assert drv.status == 401


@pytest.mark.asyncio
async def test_sse_last_event_id_resume(db_session):
    """Reconnect with Last-Event-Id replays missed events from the ring buffer."""
    reset_fanout()
    user, cb = _make_user_and_cookbook(db_session)

    eid1 = await publish_event(str(cb.id), {"slug": "a", "version": "1"})
    eid2 = await publish_event(str(cb.id), {"slug": "b", "version": "2"})
    eid3 = await publish_event(str(cb.id), {"slug": "c", "version": "3"})

    app = _build_app(db_session, api_key_user_id=user.id)
    headers = [(b"last-event-id", str(eid1).encode())]

    async with SSEDriver(app, f"/api/cookbooks/{cb.id}/sync/sse", headers=headers) as drv:
        assert drv.status == 200

        replayed_ids: list[int] = []
        replayed_slugs: list[str] = []
        # Two replayed events × 4 lines each + 3 lines of initial heartbeat = 11
        lines = await drv.read_lines(11)
        for line in lines:
            if line.startswith("id: "):
                replayed_ids.append(int(line.split(": ", 1)[1]))
            elif line.startswith("data: ") and line != "data: {}":
                replayed_slugs.append(json.loads(line[len("data: "):])["slug"])
        assert replayed_ids == [eid2, eid3]
        assert replayed_slugs == ["b", "c"]
        # The trailing heartbeat after the replay confirms the loop entered.
        assert "event: ping" in lines
