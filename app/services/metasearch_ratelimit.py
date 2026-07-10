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
#
# COUNCIL FINDING 2 + R2 (2026-07-10) — HONEST P0 SCOPE, NO OVERCLAIM: these
# buckets/breakers are module-local, so they enforce a per-WORKER ceiling, NOT a
# fleet-wide one. A per-worker token bucket CANNOT share an open circuit across
# workers or atomically enforce a global token budget — that is fundamentally the
# P5 Redis job, not something a process-local dict can do. What P0 DOES provide:
#   1. Each worker independently rate-limits + circuit-breaks its OWN upstream
#      calls (real protection against one worker hammering a source).
#   2. ``effective_limits`` divides each source's configured ceiling by the worker
#      count (best-effort: at the real 2-worker deploy, aggregate ≈ the single
#      ceiling; it is NOT a guarantee at very high worker counts — see the floor
#      note on effective_limits).
# The real deploy (recipes-api.service / wiserecipes-api.service) runs uvicorn
# --workers 2. At 2 workers, ClawHub's per-worker cap is 15 burst / 4/s → ~30
# burst / 8/s aggregate, ~2 orders of magnitude under its published 3000/window.
# So P0 is SAFE at the real deploy; the global-ceiling GUARANTEE is the P5 gate.
REDIS_TODO = (
    "P5: back TokenBucket with Redis (INCRBY + EXPIRE) to enforce a GLOBAL "
    "per-source ceiling + shared circuit state across workers. P0 = per-worker."
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
            # Per-worker-adjusted limits (council finding 2): divide the source
            # ceiling by worker_count so N workers don't collectively exceed it.
            cap, refill = effective_limits(source)
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


def worker_count() -> int:
    """Best-effort worker count, defaulting to 1. Reads (in order): the
    WEB_CONCURRENCY / GUNICORN_WORKERS / UVICORN_WORKERS env vars, then the actual
    ``--workers N`` argument on the process command line (council R2: the real
    services launch ``uvicorn --workers 2`` WITHOUT setting an env var, so an
    env-only reader silently defaults to 1 and the division no-ops in prod).

    Used to divide per-source ceilings across workers so the AGGREGATE stays near
    the upstream ceiling without a shared store (the P0 best-effort; the P5 Redis
    limiter is the real global guarantee)."""
    import os
    import sys

    for var in ("WEB_CONCURRENCY", "GUNICORN_WORKERS", "UVICORN_WORKERS"):
        val = os.environ.get(var)
        if val:
            try:
                n = int(val)
                if n > 0:
                    return n
            except ValueError:
                continue
    # Fall back to the actual --workers CLI arg (uvicorn/gunicorn).
    argv = sys.argv
    for i, tok in enumerate(argv):
        if tok in ("--workers", "-w") and i + 1 < len(argv):
            try:
                n = int(argv[i + 1])
                if n > 0:
                    return n
            except ValueError:
                continue
        if tok.startswith("--workers="):
            try:
                n = int(tok.split("=", 1)[1])
                if n > 0:
                    return n
            except ValueError:
                continue
    return 1


def effective_limits(source: str) -> tuple[float, float]:
    """The per-worker (capacity, refill) after dividing the source ceiling by the
    worker count. Multi-worker deployments thus keep their AGGREGATE upstream rate
    near the single-source ceiling instead of N× it. When workers=1 this is the
    raw configured limit.

    NOTE (council R2): the floor at (1.0, 0.1) means the division does NOT hold
    the aggregate at very high worker counts (e.g. 100 workers × floor-1.0 = 100
    burst). That is an accepted P0 limitation — the real deploy is 2 workers, and
    the true global guarantee is the P5 Redis limiter. The floor exists so a
    high worker count never zeroes a source out entirely."""
    cap, refill = _SOURCE_LIMITS.get(source, (_DEFAULT_CAPACITY, _DEFAULT_REFILL_PER_SEC))
    workers = max(1, worker_count())
    return (max(1.0, cap / workers), max(0.1, refill / workers))
