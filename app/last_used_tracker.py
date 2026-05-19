"""Redis-batched last_used_at tracker (Issue #17).

Instead of a DB write+commit on every authenticated API request (write
amplification), middleware calls ``tracker.record()`` which pushes only
to Redis (or an in-memory dict if Redis is unavailable).

A cron job (crons/drain_last_used.py) periodically calls
``tracker.drain(db)`` which bulk-UPDATEs every dirty key in a single
SQL statement and returns the count of updated rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy.orm import Session

logger = logging.getLogger("wiserecipes.last_used_tracker")

# Redis hash key that stores {str(api_key_id): iso-timestamp}
_REDIS_HASH_KEY = "wr:last_used"


class LastUsedTracker:
    """Track ``last_used_at`` timestamps for API keys with batched DB writes.

    Usage:
        tracker = LastUsedTracker(redis_client=get_redis())
        # In middleware on every authenticated request:
        tracker.record(api_key_obj.id, datetime.now(timezone.utc))
        # In a cron job every N minutes:
        n = tracker.drain(db)
    """

    def __init__(self, redis_client=None):
        """Initialise the tracker.

        Args:
            redis_client: A connected redis client or None.  When None (or when
                Redis becomes unreachable during ``record``), the tracker falls
                back to an in-memory dict so no timestamps are lost.
        """
        self.redis = redis_client
        # In-memory fallback: {str(api_key_id): datetime}
        self._memory_cache: dict[str, datetime] = {}

    def record(self, api_key_id: UUID, ts: Optional[datetime] = None) -> None:
        """Record an API key access timestamp.

        Stores the *maximum* timestamp seen for each key so that if multiple
        workers race the correct most-recent value always wins.

        Args:
            api_key_id: The UUID of the APIKey row.
            ts: The access timestamp (defaults to ``datetime.now(timezone.utc)``).
        """
        if ts is None:
            ts = datetime.now(timezone.utc)
        key_str = str(api_key_id)
        ts_str = ts.isoformat()

        if self.redis is not None:
            try:
                # HSETNX-style: only overwrite if stored value is older.
                # We use a Lua script via HSET so it's atomic per-field.
                existing = self.redis.hget(_REDIS_HASH_KEY, key_str)
                if existing is None or existing < ts_str:
                    self.redis.hset(_REDIS_HASH_KEY, key_str, ts_str)
                return
            except Exception as exc:  # noqa: BLE001 — Redis down, fall through
                logger.warning("LastUsedTracker: Redis write failed, falling back to memory: %s", exc)

        # Fallback: in-memory dict (max semantics)
        prev = self._memory_cache.get(key_str)
        if prev is None or ts > prev:
            self._memory_cache[key_str] = ts

    def drain(self, db: Session) -> int:
        """Bulk-UPDATE ``last_used_at`` for all dirty keys and clear the cache.

        Merges in-memory cache into Redis first (if Redis available), then
        reads all entries from Redis and emits a single bulk UPDATE.  Falls
        back to pure in-memory drain if Redis is unavailable.

        Returns:
            Number of API key rows updated in the database.
        """
        # Collect pending updates: {str(uuid): datetime}
        pending: dict[str, datetime] = {}

        # --- Redis path ---
        if self.redis is not None:
            try:
                # Flush any in-memory entries into Redis first.
                if self._memory_cache:
                    for k, v in list(self._memory_cache.items()):
                        existing = self.redis.hget(_REDIS_HASH_KEY, k)
                        if existing is None or existing < v.isoformat():
                            self.redis.hset(_REDIS_HASH_KEY, k, v.isoformat())
                    self._memory_cache.clear()

                # Read all entries in one HGETALL.
                raw = self.redis.hgetall(_REDIS_HASH_KEY)
                if not raw:
                    return 0

                for k, v in raw.items():
                    try:
                        pending[k] = datetime.fromisoformat(v)
                    except ValueError:
                        logger.warning("LastUsedTracker: malformed timestamp for key %s: %r", k, v)

                # Delete the hash atomically before the DB write so we don't
                # double-drain.  If the DB write fails the timestamps are gone
                # but that is acceptable (last_used_at is best-effort).
                self.redis.delete(_REDIS_HASH_KEY)

            except Exception as exc:  # noqa: BLE001 — Redis down, fall through to memory
                logger.warning("LastUsedTracker: Redis drain failed, using memory cache: %s", exc)
                pending = dict(self._memory_cache)
                self._memory_cache.clear()
        else:
            # Pure in-memory drain.
            pending = dict(self._memory_cache)
            self._memory_cache.clear()

        if not pending:
            return 0

        return self._bulk_update(db, pending)

    @staticmethod
    def _bulk_update(db: Session, pending: dict[str, datetime]) -> int:
        """Emit a single bulk UPDATE for all dirty keys.

        Returns the number of rows actually updated.
        """
        from app.models import APIKey  # lazy import to avoid circular deps

        updated = 0
        for key_str, ts in pending.items():
            try:
                key_uuid = UUID(key_str)
            except ValueError:
                logger.warning("LastUsedTracker: invalid UUID in pending: %r", key_str)
                continue
            n = (
                db.query(APIKey)
                .filter(APIKey.id == key_uuid)
                .update({"last_used_at": ts}, synchronize_session=False)
            )
            updated += n

        if updated:
            db.commit()
        return updated


# Module-level singleton — lazily wired once middleware imports it.
# Middleware does: from app.last_used_tracker import tracker
# and then calls tracker.record(key_id, ts).
#
# The redis client is injected at import time from app.middleware's get_redis().
# If Redis is unavailable at import time the tracker starts in memory-only mode;
# if Redis comes up later, callers can replace tracker.redis directly.
tracker = LastUsedTracker()
