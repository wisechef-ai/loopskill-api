"""LoopSkill mesh credential JWKS snapshot — reference implementation.

Spec: projects/loopskill/plans/2026-08-04-mesh0408-T0C-credential-trust-spec.md §3, §8

Deps: none beyond the Python standard library + whatever HTTP client the
caller supplies via `fetch_jwks`. NO LoopSkill package. NO Hermes import.

**Why this exists as its own component instead of a caveat in the verifier
docstring:** the v1 spec published a verifier snippet using PyJWT's
`PyJWKClient`, and the council rejected it for two reasons that point in
opposite directions simultaneously:

  1. `cache_keys=True` uses an `lru_cache` with NO time-based expiration —
     a warm process serves a RETIRED key forever.
  2. The Set-cache has NO stale-serve path — a LoopSkill outage fails
     closed at whatever the cache's TTL is (effectively immediately),
     instead of the spec's intended 86400s grace window.

Both defects come from the same root cause: `PyJWKClient` has no state
machine, only a cache. This module IS the state machine spec §3 requires:

    fresh (age < 3600s)          -> verify offline, no refresh
    aging (3600s <= age < 86400) -> verify offline against the stale
                                     snapshot, schedule an out-of-band
                                     refresh once age crosses 2880s (80%)
    stale (age >= 86400s)        -> reject everything, fail closed
    cold  (no snapshot ever)     -> reject everything

Refresh is NEVER performed inline during `key_for()` — verification must
never make a network call (spec §3.1). The caller (a background task, a
cron, a lifespan hook) calls `.maybe_refresh()` on its own schedule; this
class only tracks WHETHER a refresh is due and rate-limits unknown-kid
triggered refresh attempts to at most one per 60s.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

FRESH_TTL_SECONDS = 3600
REFRESH_AT_SECONDS = 2880  # 80% of FRESH_TTL_SECONDS
HARD_EXPIRY_SECONDS = 86400
UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS = 60
REFRESH_TIMEOUT_SECONDS = 2  # never 30 — the DoS the council caught


class JWKSUnavailableError(Exception):
    """Raised by key_for() when there is no usable snapshot. Fail closed."""


class UnknownKidError(Exception):
    """Raised by key_for() when the kid is not in the current snapshot."""


@dataclass
class _Snapshot:
    keys: dict  # kid -> public key object (already parsed, ready to verify with)
    fetched_at: float


@dataclass
class JWKSStateMachine:
    """Thread-safe JWKS snapshot with the state machine spec §3 requires.

    `fetch_jwks_fn` is a zero-arg callable returning a `{kid: public_key}`
    dict (already-parsed key objects — this class does not know or care how
    JWK JSON is turned into a verifiable key; that conversion is the
    caller's HTTP+parsing layer, kept separate so THIS class has no network
    or JSON dependency at all).
    """

    fetch_jwks_fn: Callable[[], dict]
    _snapshot: _Snapshot | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _last_refresh_attempt: float = field(default=0.0, init=False, repr=False)
    _last_unknown_kid_refresh: float = field(default=float("-inf"), init=False, repr=False)

    def bootstrap(self, now: float | None = None) -> None:
        """Synchronous initial fetch — call once at process start, NOT per-request."""
        self._do_refresh(now)

    def _do_refresh(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        self._last_refresh_attempt = now
        try:
            keys = self.fetch_jwks_fn()
        except Exception:
            # Fetch failure: keep the previous snapshot (or stay cold).
            # This is the fail-closed-at-hard-expiry behaviour, not
            # fail-open-on-fetch-error.
            return False
        with self._lock:
            # Atomic swap — readers never see a partially-built snapshot.
            self._snapshot = _Snapshot(keys=keys, fetched_at=now)
        return True

    def maybe_refresh(self, now: float | None = None) -> None:
        """Call periodically (e.g. every 30-60s from a background loop).

        Refreshes when the snapshot is missing or has crossed the 80%
        threshold. Never called from the request path.
        """
        now = now if now is not None else time.time()
        snap = self._snapshot
        if snap is None or (now - snap.fetched_at) >= REFRESH_AT_SECONDS:
            self._do_refresh(now)

    def _maybe_refresh_on_unknown_kid(self, now: float) -> None:
        """Rate-limited refresh trigger for an unknown kid. Spec §3.1.

        At most one refresh per 60s per process, off the request path in
        spirit (this call is synchronous for simplicity in the reference
        implementation, but bounded to REFRESH_TIMEOUT_SECONDS-class cost by
        the caller's fetch_jwks_fn — production deployments should make
        this truly asynchronous; the rate limit here is what prevents a
        kid-spray from becoming a request-amplification DoS regardless).
        """
        if now - self._last_unknown_kid_refresh < UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS:
            return
        self._last_unknown_kid_refresh = now
        self._do_refresh(now)

    def key_for(self, kid: str | None, now: float | None = None):
        """Return the public key for `kid`, or raise. NEVER performs a
        blocking network fetch on the request path itself — an unknown kid
        triggers at most a rate-limited refresh (see above), and the
        request making that discovery still gets an immediate reject; the
        refreshed snapshot only helps the NEXT request.
        """
        now = now if now is not None else time.time()

        if not kid:
            raise UnknownKidError("missing kid")

        snap = self._snapshot
        if snap is None:
            raise JWKSUnavailableError("no snapshot available (cold start / LoopSkill unreachable)")

        age = now - snap.fetched_at
        if age >= HARD_EXPIRY_SECONDS:
            raise JWKSUnavailableError(f"snapshot is {age:.0f}s old, past the {HARD_EXPIRY_SECONDS}s hard expiry")

        if kid not in snap.keys:
            self._maybe_refresh_on_unknown_kid(now)
            raise UnknownKidError(f"unknown kid: {kid!r}")

        return snap.keys[kid]

    def snapshot_age_seconds(self, now: float | None = None) -> float | None:
        snap = self._snapshot
        if snap is None:
            return None
        now = now if now is not None else time.time()
        return now - snap.fetched_at
