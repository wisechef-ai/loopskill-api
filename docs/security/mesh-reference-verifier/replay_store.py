"""LoopSkill mesh credential replay store — reference implementation.

Spec §5: "The `jti` store must be shared across worker processes and hosts.
N in-memory caches behind a load balancer means N-fold replay. Redis or
equivalent; an in-process dict is conformant ONLY for a single-process
receiver, and the docs must say so."

This in-memory implementation is that single-process fallback, explicitly
NOT production-conformant for a multi-process/multi-host receiver. It
exists so the reference verifier and its tests are runnable with zero
external services. A production receiver MUST replace this with Redis (or
equivalent) using the same `insert_if_absent` contract — atomic
insert-if-absent, TTL-bound, per spec §5's "codex B10" finding: naive
check-then-set lets two concurrent requests both observe "not present" and
both admit, which is exactly the replay this store exists to prevent.
"""

from __future__ import annotations

import threading
import time


class InMemoryReplayStore:
    """NOT multi-process safe. See module docstring."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def insert_if_absent(self, jti: str, ttl: int, now: float | None = None) -> bool:
        """Atomically record `jti` if not already present. Returns True if
        this call recorded it (i.e. this is the first time it's been seen),
        False if it was already present (replay).
        """
        now = now if now is not None else time.time()
        with self._lock:
            self._evict_expired(now)
            if jti in self._seen:
                return False
            self._seen[jti] = now + ttl
            return True

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, exp in self._seen.items() if exp <= now]
        for k in expired:
            del self._seen[k]
