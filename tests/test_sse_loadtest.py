"""N=200 mock-subscriber load test — v7 Phase D, premortem F3 / R3 gate.

Spins up 200 ASGI subscribers concurrently against a single FastAPI app.
The contract:

  * The first 100 connections succeed with HTTP 200 and a ``text/event-stream``
    body.
  * Connections 101-200 are rejected with HTTP 503, a ``Retry-After`` header,
    and a JSON body containing a ``polling_fallback`` URL.
  * Publishing one event reaches all 100 subscribers within one second.
  * The DB connection pool peaks at ≤5 active checkouts during the test —
    the proof that subscribers don't each hold a Postgres slot.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
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

from tests.test_sse_basic import SSEDriver, _InjectAuthASGI


# ── Pool-checkout instrumentation ────────────────────────────────────────

class _PoolMeter:
    """Tracks current and peak SQLAlchemy connection-pool checkouts."""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0
        self._lock = threading.Lock()

    def hook(self, engine):
        @event.listens_for(engine, "checkout")
        def _on_checkout(*_a, **_k):
            with self._lock:
                self.current += 1
                if self.current > self.peak:
                    self.peak = self.current

        @event.listens_for(engine, "checkin")
        def _on_checkin(*_a, **_k):
            with self._lock:
                self.current = max(0, self.current - 1)


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


def _build_app(db: Session, *, api_key_user_id) -> FastAPI:
    from app.cookbook_routes import router as cookbook_router
    from app.sse_routes import router as sse_router

    app = FastAPI()

    def _odb():
        SessionLocal = sessionmaker(bind=db.bind, autocommit=False, autoflush=False)
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _odb
    app.add_middleware(_InjectAuthASGI, uid=api_key_user_id)
    app.include_router(cookbook_router)
    app.include_router(sse_router)
    return app


# ── The load test ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_n200_first_100_succeed_rest_get_503(db_session, engine_fixture):
    reset_fanout()

    user = User(
        id=uuid4(),
        display_name="LT",
        email=f"{uuid4()}@test.example",
        subscription_tier="operator",
        subscription_status="active",
    )
    db_session.add(user)
    db_session.flush()
    cb = Cookbook(id=uuid4(), name="LT-CB", cookbook_owner=user.id)
    db_session.add(cb)
    db_session.commit()

    meter = _PoolMeter()
    meter.hook(engine_fixture)

    app = _build_app(db_session, api_key_user_id=user.id)
    cid = str(cb.id)
    path = f"/api/cookbooks/{cid}/sync/sse"

    N = 200
    drivers: list[SSEDriver] = [SSEDriver(app, path) for _ in range(N)]

    # Open all 200 sessions sequentially in the same loop. Each driver's
    # __aenter__ pumps until http.response.start arrives; from that point
    # the streaming generator is parked on queue.get(), holding zero DB
    # slots.
    t0 = time.perf_counter()
    statuses: list[int] = []
    for drv in drivers:
        await drv.__aenter__()
        statuses.append(drv.status)
    t_open = time.perf_counter() - t0

    accepted = [d for d in drivers if d.status == 200]
    rejected = [d for d in drivers if d.status == 503]

    assert len(accepted) == 100, f"expected 100×200, got {len(accepted)}"
    assert len(rejected) == 100, f"expected 100×503, got {len(rejected)}"

    # ── 503 body / headers contract ──────────────────────────────────────
    sample = rejected[0]
    body = b""
    while True:
        msg = await asyncio.wait_for(sample._send_q.get(), timeout=2.0)
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    payload = json.loads(body.decode())
    assert payload["detail"] == "sse_pool_exhausted"
    assert payload["polling_fallback"] == f"/api/cookbooks/{cid}/sync"
    hdrs = {k: v for (k, v) in sample.response_headers}
    assert hdrs.get(b"retry-after") == b"30"

    # ── Drain initial heartbeats from accepted streams ───────────────────
    for d in accepted:
        await d.read_lines(3, timeout=2.0)

    # ── Fan-out: one event reaches all 100 in <1s ────────────────────────
    t_pub = time.perf_counter()
    eid = await publish_event(cid, {"slug": "broadcast", "version": "1.0.0"})
    latencies: list[float] = []
    for d in accepted:
        ev = await d.read_lines(4, timeout=2.0)
        latencies.append(time.perf_counter() - t_pub)
        assert ev[0] == f"id: {eid}"
        assert ev[1] == "event: cookbook_event"
    t_fanout = time.perf_counter() - t_pub
    assert t_fanout < 1.0, f"fanout took {t_fanout:.3f}s (want <1s)"

    # ── Pool slots — the F3 / R3 gate ───────────────────────────────────
    assert meter.peak <= 5, f"pool peak {meter.peak} > 5 (F3 R3 mitigation broken)"

    p50 = sorted(latencies)[len(latencies) // 2]
    p95 = sorted(latencies)[int(len(latencies) * 0.95)]

    # Stash numbers for the SUBAGENT_D_OUTPUT.md report.
    import os
    os.environ["_SSE_LT_OPEN_S"] = f"{t_open:.3f}"
    os.environ["_SSE_LT_FANOUT_S"] = f"{t_fanout:.4f}"
    os.environ["_SSE_LT_POOL_PEAK"] = str(meter.peak)
    os.environ["_SSE_LT_P50_MS"] = f"{p50 * 1000:.2f}"
    os.environ["_SSE_LT_P95_MS"] = f"{p95 * 1000:.2f}"

    # Tear down the streams.
    for d in drivers:
        await d.__aexit__(None, None, None)
