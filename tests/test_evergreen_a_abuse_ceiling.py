"""evergreen_0206 Phase A — per-agent reconcile abuse ceiling.

Pins the decision-#20 contract: the reconcile rate limit is a per-`api_key_id`
abuse ceiling, IDENTICAL across all tiers, generous enough that honest clients
never trip it, and fail-open on Redis outage. It is NOT a tier speed-throttle.
"""

from __future__ import annotations

import pytest

from app.reconcile_abuse_ceiling import (
    RECONCILE_CEILING_REQUESTS,
    RECONCILE_CEILING_WINDOW_SECONDS,
    check_reconcile_abuse_ceiling,
    _reset_for_tests,
)


@pytest.fixture(autouse=True)
def _force_memory_path(monkeypatch):
    """Force the in-memory fallback so the suite is deterministic without Redis.

    get_redis() returning None routes check_reconcile_abuse_ceiling through the
    in-memory sliding window — same algorithm, no external dependency.
    """
    import app.middleware as mw

    monkeypatch.setattr(mw, "get_redis", lambda: None)
    _reset_for_tests()
    yield
    _reset_for_tests()


class TestPerAgentCeiling:
    def test_normal_usage_never_trips(self):
        """A handful of reconciles is always allowed."""
        for i in range(5):
            r = check_reconcile_abuse_ceiling("key-normal", now=1000.0 + i)
            assert r.allowed, f"request {i} should be allowed"

    def test_exactly_at_ceiling_then_blocked(self):
        """The (N+1)th request inside the window is blocked with Retry-After."""
        t = 2000.0
        for i in range(RECONCILE_CEILING_REQUESTS):
            r = check_reconcile_abuse_ceiling("key-spam", now=t + i * 0.01)
            assert r.allowed, f"request {i + 1}/{RECONCILE_CEILING_REQUESTS} should be allowed"
        # The next one, still inside the window, trips.
        blocked = check_reconcile_abuse_ceiling("key-spam", now=t + 1.0)
        assert not blocked.allowed, "request over the ceiling must be blocked"
        assert blocked.retry_after == RECONCILE_CEILING_WINDOW_SECONDS
        assert blocked.count >= RECONCILE_CEILING_REQUESTS

    def test_window_slides_and_recovers(self):
        """After the window passes, the key is allowed again."""
        t = 3000.0
        for i in range(RECONCILE_CEILING_REQUESTS):
            check_reconcile_abuse_ceiling("key-recover", now=t + i * 0.01)
        # Blocked immediately after.
        assert not check_reconcile_abuse_ceiling("key-recover", now=t + 1.0).allowed
        # Well past the window → old hits expired → allowed.
        later = t + RECONCILE_CEILING_WINDOW_SECONDS + 10
        assert check_reconcile_abuse_ceiling("key-recover", now=later).allowed

    def test_ceiling_is_per_key_not_shared(self):
        """One key spamming does NOT affect a different key (no shared bucket)."""
        t = 4000.0
        for i in range(RECONCILE_CEILING_REQUESTS):
            check_reconcile_abuse_ceiling("key-A", now=t + i * 0.01)
        assert not check_reconcile_abuse_ceiling("key-A", now=t + 1.0).allowed
        # key-B is untouched.
        assert check_reconcile_abuse_ceiling("key-B", now=t + 1.0).allowed


class TestTierIndependence:
    """Decision #20: the ceiling is identical for every tier — no speed-throttle.

    The function takes only api_key_id, not tier, by design: there is no tier
    parameter to vary. This test documents that invariant structurally.
    """

    def test_no_tier_parameter_exists(self):
        import inspect

        sig = inspect.signature(check_reconcile_abuse_ceiling)
        params = set(sig.parameters)
        assert "tier" not in params, (
            "the abuse ceiling must NOT take a tier — it is identical for all "
            "tiers (decision #20). Tiers separate on capability, never speed."
        )
        assert params == {"api_key_id", "now"}


class TestFailOpen:
    def test_empty_key_allowed(self):
        """No api_key_id → allow (ownership gating handles auth elsewhere)."""
        r = check_reconcile_abuse_ceiling("", now=5000.0)
        assert r.allowed
        assert r.count == 0


class _FakeRedisPipeline:
    """Minimal pipeline mimicking the zremrangebyscore/zcard/zadd/expire chain."""

    def __init__(self, store: dict, key_count: int):
        self._store = store
        self._key_count = key_count
        self._ops = 0

    def zremrangebyscore(self, *a, **k):
        self._ops += 1
        return self

    def zcard(self, *a, **k):
        self._ops += 1
        return self

    def zadd(self, *a, **k):
        self._ops += 1
        return self

    def expire(self, *a, **k):
        self._ops += 1
        return self

    def execute(self):
        # Mirror the real pipeline result order: [zrem, zcard, zadd, expire].
        return [0, self._key_count, 1, True]


class _FakeRedis:
    def __init__(self, key_count: int):
        self._key_count = key_count

    def pipeline(self):
        return _FakeRedisPipeline({}, self._key_count)


class TestRedisPath:
    """Exercise the Redis branch (coverage of _check_redis) with a fake client."""

    def test_redis_allows_under_ceiling(self, monkeypatch):
        import app.middleware as mw

        # zcard returns a count below the ceiling → allowed.
        monkeypatch.setattr(mw, "get_redis", lambda: _FakeRedis(key_count=5))
        r = check_reconcile_abuse_ceiling("key-redis-ok", now=9000.0)
        assert r.allowed
        assert r.count == 5
        assert r.retry_after == 0

    def test_redis_blocks_at_ceiling(self, monkeypatch):
        import app.middleware as mw

        # zcard returns the ceiling → blocked with Retry-After.
        monkeypatch.setattr(mw, "get_redis", lambda: _FakeRedis(key_count=RECONCILE_CEILING_REQUESTS))
        r = check_reconcile_abuse_ceiling("key-redis-block", now=9001.0)
        assert not r.allowed
        assert r.count == RECONCILE_CEILING_REQUESTS
        assert r.retry_after == RECONCILE_CEILING_WINDOW_SECONDS

    def test_redis_failure_fails_open_to_memory(self, monkeypatch):
        """A Redis ConnectionError must fall through to the memory path (allow)."""
        import redis as _redis

        import app.middleware as mw

        class _BoomRedis:
            def pipeline(self):
                raise _redis.ConnectionError("simulated outage")

        monkeypatch.setattr(mw, "get_redis", lambda: _BoomRedis())
        monkeypatch.setattr(mw, "mark_redis_failed", lambda: None)
        # Should not raise; falls open to the in-memory window → allowed.
        r = check_reconcile_abuse_ceiling("key-redis-fail", now=9002.0)
        assert r.allowed
