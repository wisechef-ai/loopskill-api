"""In-process fan-out for cookbook sync events — v7 Phase D.

Two transports:

  * Production (PostgreSQL): a single ``LISTEN cookbook_events`` connection
    is opened on app startup. Publishers emit ``pg_notify('cookbook_events',
    '<json>')`` from inside the request transaction. The single LISTEN
    connection receives every NOTIFY (one socket, regardless of how many
    SSE subscribers are connected) and fans out to in-memory subscriber
    queues — this is the F3 / R3 mitigation that keeps the connection pool
    drained under load.

  * Tests (SQLite): SQLite has no LISTEN/NOTIFY, so publishers call
    :func:`publish_event` directly. The same in-memory subscriber registry
    is used, so SSE handlers behave identically.

The publisher should always go through :func:`emit_cookbook_event`, which
selects the right transport based on the database dialect.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict, deque
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class Fanout:
    """In-process subscriber registry plus optional Postgres LISTEN worker."""

    BACKLOG = 100  # per-cookbook ring buffer for Last-Event-Id resume

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._backlog: dict[str, deque[tuple[int, dict]]] = defaultdict(
            lambda: deque(maxlen=self.BACKLOG)
        )
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._listener_conn: Any = None

    async def subscribe(self, cookbook_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        async with self._lock:
            self._subs[str(cookbook_id)].add(q)
        return q

    async def unsubscribe(self, cookbook_id: str, q: asyncio.Queue) -> None:
        async with self._lock:
            subs = self._subs.get(str(cookbook_id))
            if subs and q in subs:
                subs.discard(q)
                if not subs:
                    self._subs.pop(str(cookbook_id), None)

    async def publish(self, cookbook_id: str, event: dict) -> int:
        cid = str(cookbook_id)
        async with self._lock:
            self._next_id += 1
            event_id = self._next_id
            self._backlog[cid].append((event_id, event))
            subs = list(self._subs.get(cid, ()))
        envelope = {"id": event_id, "data": event}
        for q in subs:
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                logger.warning("fanout: subscriber queue full for cookbook %s", cid)
        return event_id

    def replay_since(self, cookbook_id: str, last_event_id: int) -> list[dict]:
        cid = str(cookbook_id)
        return [
            {"id": eid, "data": data}
            for (eid, data) in list(self._backlog.get(cid, ()))
            if eid > last_event_id
        ]

    def subscriber_count(self, cookbook_id: str) -> int:
        return len(self._subs.get(str(cookbook_id), ()))

    async def start_listener(self) -> None:
        url = os.environ.get("DATABASE_URL", "")
        if not url.startswith("postgres"):
            logger.info("fanout: non-postgres DB, LISTEN/NOTIFY worker not started")
            return
        try:
            import asyncpg  # type: ignore
        except ImportError:
            logger.warning("fanout: asyncpg not installed; LISTEN/NOTIFY worker disabled")
            return
        asyncpg_url = url
        for prefix in ("postgresql+psycopg2://", "postgresql+asyncpg://", "postgres://"):
            if asyncpg_url.startswith(prefix):
                asyncpg_url = "postgresql://" + asyncpg_url[len(prefix):]
                break
        self._listener_conn = await asyncpg.connect(asyncpg_url)
        await self._listener_conn.add_listener("cookbook_events", self._on_notify)
        logger.info("fanout: LISTEN cookbook_events established (single connection)")

    def _on_notify(self, _conn, _pid, _channel, payload: str) -> None:
        try:
            evt = json.loads(payload)
        except Exception:
            logger.exception("fanout: bad NOTIFY payload %r", payload)
            return
        cookbook_ids = evt.get("cookbooks") or []
        for cid in cookbook_ids:
            asyncio.create_task(self.publish(str(cid), evt))

    async def stop_listener(self) -> None:
        if self._listener_conn is not None:
            try:
                await self._listener_conn.close()
            except Exception:
                logger.exception("fanout: error closing listener connection")
            self._listener_conn = None


_fanout: Fanout | None = None


def get_fanout() -> Fanout:
    global _fanout
    if _fanout is None:
        _fanout = Fanout()
    return _fanout


def reset_fanout() -> None:
    """Test helper — drop all subscribers and reset event id counter."""
    global _fanout
    _fanout = Fanout()


async def publish_event(cookbook_id: str, event: dict) -> int:
    """Direct fan-out to local subscribers (used by tests / non-postgres)."""
    return await get_fanout().publish(str(cookbook_id), event)


def _is_postgres(db: Session) -> bool:
    try:
        return db.bind.dialect.name == "postgresql"
    except Exception:
        return False


async def emit_cookbook_event(db: Session, cookbook_ids: list[str], event: dict) -> None:
    """Send a cookbook event to every cookbook in ``cookbook_ids``.

    On Postgres, emits ``pg_notify('cookbook_events', ...)`` so that *all*
    processes (including the publishing one) see the event via their LISTEN
    worker — keeping fan-out single-pathed.

    On non-Postgres (SQLite tests), publishes directly to the in-process
    subscriber registry.
    """
    if not cookbook_ids:
        return
    payload = {**event, "cookbooks": [str(c) for c in cookbook_ids]}
    if _is_postgres(db):
        try:
            db.execute(
                text("SELECT pg_notify('cookbook_events', :p)"),
                {"p": json.dumps(payload)},
            )
        except Exception:
            logger.exception("fanout: pg_notify failed")
    else:
        for cid in cookbook_ids:
            await publish_event(str(cid), event)
