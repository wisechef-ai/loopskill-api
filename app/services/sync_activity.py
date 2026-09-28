"""paywall_0925 — record that a user's agents synced on a given UTC day.

Feeds ``repeat_sync_users_30d`` on /api/admin/pulse: the number of users with
syncs on >= 2 distinct days in the last 30 days (repeat use is the leading
indicator of a user who will feel the maintenance ceiling and pay).

Nothing else records "user X synced": reconcile_events are client-emitted
canary outcomes, loop_runs are fleet telemetry, and the reconcile poll itself
persists nothing. So every sync surface calls ``record_sync_day``:

* ``POST /api/bundles/{id}/reconcile`` — the thin-client poll (incl. 304s)
* the ``loopskill_sync`` MCP verb
* ``POST /api/sync-report`` — the fleet member's batched cycle report

Cost discipline: the reconcile poll's 304 fast path is ~99% of traffic and
must stay ONE indexed lookup. A process-local seen-set short-circuits every
call after the first for a (user, day), so the steady state is zero extra
queries; the table is O(users x days), written with insert-if-absent.
Never raises — sync must not fail because telemetry did.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.services._side_session import side_session

logger = logging.getLogger(__name__)

SOURCE_RECONCILE = "reconcile"
SOURCE_MCP_SYNC = "mcp_sync"
SOURCE_SYNC_REPORT = "sync_report"

_SEEN_MAX = 50_000
_seen: set[tuple[UUID, date]] = set()
_seen_lock = threading.Lock()


def _clear_seen_cache() -> None:
    """Test hook — forget which (user, day) pairs were already written."""
    with _seen_lock:
        _seen.clear()


def record_sync_day(
    db: Session,
    user_id: UUID | str | None,
    *,
    source: str,
    now: datetime | None = None,
) -> None:
    """Idempotently mark (user_id, today) as a sync day. Never raises."""
    if user_id is None:
        return  # master key / anonymous: no user to attribute repeat use to
    # Rationale: best-effort telemetry on the hot sync path — any failure is
    # logged and swallowed so a reconcile/sync never fails because of it.
    try:
        uid = user_id if isinstance(user_id, UUID) else UUID(str(user_id))
        day = (now or datetime.now(UTC)).date()
        key = (uid, day)
        with _seen_lock:
            if key in _seen:
                return
        _write(db, uid, day, source)
        with _seen_lock:
            if len(_seen) >= _SEEN_MAX:
                _seen.clear()
            _seen.add(key)
    except Exception:  # noqa: BLE001
        logger.warning("sync_activity: failed to record sync day source=%s", source, exc_info=True)


def _write(db: Session, uid: UUID, day: date, source: str) -> None:
    from app.models import UserSyncDay

    with side_session(db) as s:
        exists = s.execute(
            select(UserSyncDay.user_id).where(UserSyncDay.user_id == uid, UserSyncDay.day == day)
        ).first()
        if exists is not None:
            return
        s.add(UserSyncDay(user_id=uid, day=day, first_source=source[:32]))
        try:
            s.flush()
        except IntegrityError:
            s.rollback()  # a concurrent writer won the same (user, day): fine
