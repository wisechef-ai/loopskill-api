"""mesh0408e2e — the half-open probe must never be burned without an outcome.

WHY THIS EXISTS
    Found live on 2026-08-07: all 14 federated sources (91,105 indexed /
    20,991 installable) returned 0 results behind an HTTP 200, while the
    upstream was healthy (200 in 0.42s). Only an API process restart cleared
    it.

    ``CircuitBreaker.allow`` hands out exactly ONE half-open probe and sets
    ``_half_open_probe_inflight``. Only ``record_success``/``record_failure``
    clear it, and ``_opened_at`` is never refreshed — so once a probe is spent
    without an outcome, the cooldown check keeps passing but the inflight check
    returns False forever. The breaker never re-closes for the life of the
    process, and ``_breakers`` is a process-global dict with no TTL.

    Two paths burned the probe:
      D1  ``acquire`` consulted the breaker BEFORE the token bucket, so a
          rate-limited call that never reached the upstream still spent it.
      D2  ``_search_one`` returned on ``no_fetch_callable`` / ``no_adapter``
          before the try/except that records health.
"""

from __future__ import annotations

import time

from app.services import metasearch_ratelimit as rl
from app.services.metasearch_ratelimit import CircuitBreaker, TokenBucket

COOLDOWN = 0.05


def _tripped(cooldown: float = COOLDOWN) -> CircuitBreaker:
    br = CircuitBreaker(fail_threshold=5, cooldown_sec=cooldown)
    for _ in range(5):
        br.record_failure()
    return br


def test_recorded_outcome_recloses_the_breaker():
    """CONTROL. Without this the rest proves nothing: the harness must be able
    to observe a breaker re-closing at all."""
    br = _tripped()
    time.sleep(COOLDOWN * 2)
    assert br.allow() is True, "cooldown elapsed — a probe is due"
    br.record_success()
    assert br.allow() is True
    assert br.state == "closed"


def test_rate_limited_call_does_not_burn_the_probe():
    """D1: a call the token bucket rejects never reaches the upstream, so it
    must not consume the source's single half-open probe."""
    src = "test-d1-src"
    rl.reset_all()
    try:
        br = _tripped()
        rl._breakers[src] = br
        bucket = TokenBucket(capacity=1.0, refill_per_sec=0.0001)
        bucket.try_acquire()  # drain
        rl._buckets[src] = bucket
        time.sleep(COOLDOWN * 2)

        assert rl.acquire(src) is False, "bucket is empty — acquire must refuse"
        assert br._half_open_probe_inflight is False, (
            "the breaker was consulted before the bucket and burned the probe "
            "on a call that never happened"
        )
        # The probe survived, so the next attempt can still rehabilitate.
        assert br.allow() is True
    finally:
        rl.reset_all()


def test_breaker_recloses_after_an_unrecorded_probe():
    """D2: even if a probe IS handed out and its outcome is never recorded,
    the breaker must not stay half-open for the life of the process."""
    br = _tripped()
    time.sleep(COOLDOWN * 2)
    assert br.allow() is True  # probe consumed; caller returns early, records nothing

    time.sleep(COOLDOWN * 2)
    assert br.allow() is True, (
        "a second cooldown elapsed with no recorded outcome — the breaker must "
        "offer another probe rather than latch half-open forever"
    )
