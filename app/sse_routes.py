"""Server-Sent Events live-sync endpoint — v7 Phase D.

``GET /api/cookbooks/{id}/sync/sse`` streams ``cookbook_event`` records to
clients as they happen. Designed around the F3 / R3 pool-exhaustion
mitigation from the premortem:

  * Hard cap of :data:`SSE_MAX_CONNECTIONS` concurrent SSE streams per
    process.  The 101st client is rejected with HTTP 503 and given a
    ``polling_fallback`` URL pointing at the existing
    ``GET /api/cookbooks/{id}/sync?since=<ts>`` endpoint, plus a
    ``Retry-After`` header.

  * The SSE handler does **not** hold a Postgres pool slot for the
    duration of the connection: the only DB work is the ownership
    /tier check at the top of the handler, after which the session
    is closed explicitly.  Events flow from a single shared LISTEN
    connection (see :mod:`app.sync_fanout`) into per-subscriber asyncio
    queues.

  * Heartbeats every :data:`SSE_HEARTBEAT_SECONDS` keep proxies happy.

  * ``Last-Event-Id`` (a numeric envelope id) replays missed events from
    the in-memory ring buffer in :class:`app.sync_fanout.Fanout`.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session

from app.cookbook_routes import (
    CookbookCtx,
    _resolve_owned_cookbook,
    require_cookbook_tier,
)
from app.database import get_db
from app.sync_fanout import get_fanout

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cookbooks", tags=["live-sync"])


SSE_MAX_CONNECTIONS = 100
SSE_HEARTBEAT_SECONDS = 30


def _gate_init(app) -> None:
    if not hasattr(app.state, "sse_count"):
        app.state.sse_count = 0
    if not hasattr(app.state, "sse_lock"):
        app.state.sse_lock = asyncio.Lock()


async def _gate_acquire(app) -> bool:
    _gate_init(app)
    async with app.state.sse_lock:
        if app.state.sse_count >= SSE_MAX_CONNECTIONS:
            return False
        app.state.sse_count += 1
        return True


async def _gate_release(app) -> None:
    _gate_init(app)
    async with app.state.sse_lock:
        app.state.sse_count = max(0, app.state.sse_count - 1)


def _format_event(envelope: dict) -> str:
    return (
        f"id: {envelope['id']}\n"
        f"event: cookbook_event\n"
        f"data: {json.dumps(envelope['data'])}\n\n"
    )


@router.get("/{cookbook_id}/sync/sse")
async def cookbook_sync_sse(
    cookbook_id: str,
    request: Request,
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-Id"),
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)
    cid = str(cb.id)
    # Release the DB session before the (potentially long-lived) stream
    # starts — this is the premortem F3 / R3 mitigation.
    db.close()

    if not await _gate_acquire(request.app):
        return JSONResponse(
            status_code=503,
            content={
                "detail": "sse_pool_exhausted",
                "polling_fallback": f"/api/cookbooks/{cookbook_id}/sync",
            },
            headers={"Retry-After": "30"},
        )

    fanout = get_fanout()
    queue = await fanout.subscribe(cid)

    last_id_int = 0
    if last_event_id:
        try:
            last_id_int = int(last_event_id)
        except ValueError:
            last_id_int = 0

    async def event_stream():
        try:
            for envelope in fanout.replay_since(cid, last_id_int):
                yield _format_event(envelope)

            # Initial heartbeat so the client knows the stream is live.
            yield "event: ping\ndata: {}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    envelope = await asyncio.wait_for(
                        queue.get(), timeout=SSE_HEARTBEAT_SECONDS
                    )
                    yield _format_event(envelope)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            await fanout.unsubscribe(cid, queue)
            await _gate_release(request.app)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
