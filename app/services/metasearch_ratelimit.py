"""Per-source rate limiting + circuit breaking for the metasearch fan-out
(metasearch_0710 P0 — council condition 1).

The 2026-07-10 council (COUNCIL_TERRA/SOL) flagged that the existing federation
fan-out is (a) sequential with 12s-per-source timeouts and (b) protected only by
a process-local TTL dict — no global per-source request ceiling and no circuit
breaker. At the §7.5 scale bar (1000+ fleet runners) that cannot hold ClawHub's
published ``ratelimit-limit: 3000``/window. Both reviewers ruled a per-source
token bucket MANDATORY from P0, not P5 hardening. This module is that gate.

Two primitives, both thread-safe:

- ``TokenBucket`` — classic token bucket (capacity + refill rate). ``try_acquire``
  is non-blocking: it returns False when the bucket is dry so the caller can skip
  that source for the request (graceful degrade) rather than block the whole
  fan-out. Sized per source from the smallest published ceiling ÷ safety factor.
- ``CircuitBreaker`` — opens after N consecutive failures, stays open for a
  cooldown, then half-opens to probe. While open, ``allow()`` returns False so a
  down/slow source is dropped from the request entirely (the list still returns
  from healthy sources — plan §7 circuit breaker).

Design note on "shared, not process-local": these live at module scope so every
request in the process shares one bucket per source. A single Python worker
therefore enforces one ceiling. Multi-worker deployments still need a shared
store (Redis) to enforce a *global* ceiling — that is called out explicitly in
``REDIS_TODO`` below and in the plan's §7.5, and is a P5 hardening item. For P0
the per-worker bucket + the 5-15min result cache keeps a single box well under
3000/window; the Redis upgrade is the multi-worker generalisation, not a
correctness gap for the MVP.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# Multi-worker global ceiling requires a shared store. Tracked for P5.
REDIS_TODO = (
    "P5: back TokenBucket with Redis (INCRBY + EXPIRE) to enforce a GLOBAL "
    "per-source ceiling across gunicorn workers. P0 uses a per-worker bucket."
)


class TokenBucket:
    """Thread-safe token bucket. Non-blocking ``try_acquire``.

    capacity: max tokens (burst size). refill_per_sec: steady-state rate.
    Tokens accrue continuously up to ``capacity``.
    """

    def __init__(self, capacity: float, refill_per_sec: float) -> None:
        if capacity <= 0 or refill_per_sec <= 0:
            raise ValueError("capacity and refill_per_sec must be > 0")
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill)
            self._last = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Take ``tokens`` if available. Returns True on success, False if the
        bucket is too dry right now (caller should skip this source)."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def available(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._tokens


@dataclass
class CircuitBreaker:
    """Per-source circuit breaker.

    CLOSED  → requests allowed; failures counted.
    OPEN    → after ``fail_threshold`` consecutive failures; ``allow()`` is
              False until ``cooldown_sec`` elapses.
    HALF    → after cooldown, ONE probe allowed; success → CLOSED, failure → OPEN.
    """

    fail_threshold: int = 5
    cooldown_sec: float = 30.0
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _half_open_probe_inflight: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def allow(self) -> bool:
        """Whether a request to this source is permitted right now."""
        with self._lock:
            if self._opened_at is None:
                return True  # CLOSED
            # OPEN — check cooldown.
            if (time.monotonic() - self._opened_at) < self.cooldown_sec:
                return False
            # cooldown elapsed → allow exactly one half-open probe.
            if self._half_open_probe_inflight:
                return False
            self._half_open_probe_inflight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._opened_at = None
            self._half_open_probe_inflight = False

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._half_open_probe_inflight = False
            if self._consecutive_failures >= self.fail_threshold:
                self._opened_at = time.monotonic()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if (time.monotonic() - self._opened_at) < self.cooldown_sec:
                return "open"
            return "half_open"


# ── Per-source registry (shared at module scope) ─────────────────────────────
#
# Sized conservatively for P0. ClawHub publishes 3000/window; we cap our
# per-worker steady rate far below it. skills.sh (Vercel) has no published header
# so we stay polite. These are the ONLY knobs for the fan-out's upstream load.

_DEFAULT_CAPACITY = 20.0
_DEFAULT_REFILL_PER_SEC = 5.0

_SOURCE_LIMITS: dict[str, tuple[float, float]] = {
    # source: (capacity, refill_per_sec)
    "clawhub": (30.0, 8.0),  # published 3000/window — stay an order of magnitude under
    "skills-sh": (20.0, 5.0),
    "github-oss": (10.0, 2.0),  # anon/token GH budget is tight
    "hermes-hub": (20.0, 5.0),
    "lobehub": (20.0, 5.0),
    "browse-sh": (20.0, 5.0),
    "well-known": (20.0, 5.0),
}

_buckets: dict[str, TokenBucket] = {}
_breakers: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def _bucket_for(source: str) -> TokenBucket:
    with _registry_lock:
        b = _buckets.get(source)
        if b is None:
            cap, refill = _SOURCE_LIMITS.get(source, (_DEFAULT_CAPACITY, _DEFAULT_REFILL_PER_SEC))
            b = TokenBucket(cap, refill)
            _buckets[source] = b
        return b


def _breaker_for(source: str) -> CircuitBreaker:
    with _registry_lock:
        br = _breakers.get(source)
        if br is None:
            br = CircuitBreaker()
            _breakers[source] = br
        return br


def acquire(source: str) -> bool:
    """Gate a single upstream call to ``source``. Returns True if BOTH the
    circuit is closed/half-open AND a token is available. False → the caller
    skips this source for this request (graceful degrade)."""
    breaker = _breaker_for(source)
    if not breaker.allow():
        return False
    if not _bucket_for(source).try_acquire():
        return False
    return True


def record_success(source: str) -> None:
    _breaker_for(source).record_success()


def record_failure(source: str) -> None:
    _breaker_for(source).record_failure()


def breaker_state(source: str) -> str:
    return _breaker_for(source).state


def reset_all() -> None:
    """Test hook — clear all buckets + breakers."""
    with _registry_lock:
        _buckets.clear()
        _breakers.clear()
