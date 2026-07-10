"""Tests for metasearch rate limiting + circuit breaker (metasearch_0710 P0 —
council condition 1)."""

from __future__ import annotations

import time

from app.services import metasearch_ratelimit as rl
from app.services.metasearch_ratelimit import CircuitBreaker, TokenBucket


def test_token_bucket_allows_up_to_capacity_then_blocks():
    b = TokenBucket(capacity=3, refill_per_sec=0.0001)
    assert b.try_acquire() is True
    assert b.try_acquire() is True
    assert b.try_acquire() is True
    assert b.try_acquire() is False, "4th acquire must fail — bucket dry"


def test_token_bucket_refills_over_time():
    b = TokenBucket(capacity=1, refill_per_sec=100.0)
    assert b.try_acquire() is True
    assert b.try_acquire() is False
    time.sleep(0.05)  # 100/s → 5 tokens accrue, capped at capacity=1
    assert b.try_acquire() is True


def test_token_bucket_rejects_bad_config():
    for cap, refill in [(0, 1), (1, 0), (-1, 1)]:
        try:
            TokenBucket(cap, refill)
            assert False, f"expected ValueError for ({cap},{refill})"
        except ValueError:
            pass


def test_circuit_breaker_opens_after_threshold():
    br = CircuitBreaker(fail_threshold=3, cooldown_sec=10.0)
    assert br.allow() is True
    br.record_failure()
    br.record_failure()
    assert br.state == "closed"
    br.record_failure()  # 3rd → opens
    assert br.state == "open"
    assert br.allow() is False


def test_circuit_breaker_half_opens_after_cooldown_then_closes_on_success():
    br = CircuitBreaker(fail_threshold=1, cooldown_sec=0.05)
    br.record_failure()
    assert br.allow() is False  # open
    time.sleep(0.06)
    assert br.state == "half_open"
    assert br.allow() is True  # one probe allowed
    assert br.allow() is False  # second probe blocked while first inflight
    br.record_success()
    assert br.state == "closed"
    assert br.allow() is True


def test_circuit_breaker_reopens_on_half_open_failure():
    br = CircuitBreaker(fail_threshold=1, cooldown_sec=0.05)
    br.record_failure()
    time.sleep(0.06)
    assert br.allow() is True  # probe
    br.record_failure()  # probe failed → reopen
    assert br.state == "open"
    assert br.allow() is False


def test_acquire_gates_on_both_bucket_and_breaker():
    rl.reset_all()
    # Force a tiny bucket for a test source by pre-registering.
    src = "test-src-x"
    # Default bucket cap=20; drain it.
    drained = 0
    while rl.acquire(src):
        drained += 1
        if drained > 100:
            break
    assert drained <= 20, "default capacity should cap around 20"
    assert rl.acquire(src) is False, "dry bucket → acquire False"
    rl.reset_all()


def test_acquire_false_when_circuit_open():
    rl.reset_all()
    src = "test-src-y"
    for _ in range(5):  # default fail_threshold=5
        rl.record_failure(src)
    assert rl.breaker_state(src) == "open"
    assert rl.acquire(src) is False
    rl.reset_all()
