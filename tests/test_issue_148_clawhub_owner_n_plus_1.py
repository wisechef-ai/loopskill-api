"""Issue #148 — the ClawHub per-row owner-resolution N+1 must not reach the network.

The bug: ``ClawHubAdapter._map`` resolves an owner handle for every row, and an
uncached ``clawhub_url.resolve_owner`` call is a live
``GET clawhub.ai/api/search?q=<slug>``. ``_map`` runs once per row, so an N-row
page fires up to N sequential upstream HTTP calls. Measured on live prod: 59 s
cold (2026-07-26), re-measured >90 s / timeout (2026-07-30), against 0.62 s for
all six other sources combined.

These tests COUNT network calls rather than measuring wall-clock, so they are
deterministic in CI and cannot flake on upstream latency. The core assertion is
the N+1 shape itself: map 25 rows, assert the number of upstream lookups is 0
after priming (and demonstrably N without it).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import clawhub_url
from app.services.federation_adapters import ClawHubAdapter


@pytest.fixture(autouse=True)
def _clean_owner_cache():
    """Each test starts with an empty process-local owner cache."""
    saved = dict(clawhub_url._OWNER_CACHE)
    clawhub_url._OWNER_CACHE.clear()
    try:
        yield
    finally:
        clawhub_url._OWNER_CACHE.clear()
        clawhub_url._OWNER_CACHE.update(saved)


def _rows(n: int) -> list[dict[str, Any]]:
    """N ClawHub rows with NO ownerHandle — the shape that forces resolution."""
    return [{"slug": f"skill-{i}", "displayName": f"Skill {i}", "summary": "x"} for i in range(n)]


def test_unprimed_cache_causes_one_upstream_call_per_row(monkeypatch):
    """Baseline: this is the N+1 the issue describes. Pins the bug's shape.

    If a future change makes _map stop resolving per row, this test fails and
    tells the next reader the optimisation moved — rather than silently passing.
    """
    calls: list[str] = []

    def _fake_search(url, params=None, **kw):
        calls.append(str(params.get("q") if params else ""))
        return {"results": []}

    monkeypatch.setattr("app.services.federation_live._safe_json_get", _fake_search)

    adapter = ClawHubAdapter(fetch=lambda q: _rows(25))
    adapter.search("anything", limit=25)

    assert len(calls) == 25, f"expected the N+1 (25 upstream calls), got {len(calls)}"


def test_primed_cache_eliminates_all_upstream_calls(monkeypatch):
    """The fix: prime from the persisted snapshot -> ZERO upstream lookups."""
    calls: list[str] = []

    def _fake_search(url, params=None, **kw):
        calls.append(str(params.get("q") if params else ""))
        return {"results": []}

    monkeypatch.setattr("app.services.federation_live._safe_json_get", _fake_search)

    # What load_resolved_owner_handles(db) would return from FederationHubSkill.
    snapshot = {f"skill-{i}": f"owner{i}" for i in range(25)}
    added = clawhub_url.prime_owner_cache(snapshot)
    assert added == 25

    adapter = ClawHubAdapter(fetch=lambda q: _rows(25))
    results = adapter.search("anything", limit=25)

    assert len(calls) == 0, f"N+1 not eliminated — {len(calls)} upstream calls after priming"
    assert len(results) == 25
    # And the owner actually reached the deep link — the whole point of #139.
    assert "owner0" in results[0].origin_url


def test_priming_does_not_overwrite_a_live_resolved_value():
    """A value already in cache (live-resolved, possibly fresher) wins."""
    clawhub_url._OWNER_CACHE["skill-1"] = "live-owner"
    added = clawhub_url.prime_owner_cache({"skill-1": "snapshot-owner", "skill-2": "s2"})

    assert added == 1  # only skill-2
    assert clawhub_url._OWNER_CACHE["skill-1"] == "live-owner"


def test_priming_does_not_overwrite_a_cached_negative_result():
    """A cached ``None`` (upstream could not resolve it) must not be clobbered.

    ``resolve_owner`` caches negatives deliberately so an unresolvable slug is not
    retried on every request. Priming must respect that, or the negative-cache
    optimisation silently stops working.
    """
    clawhub_url._OWNER_CACHE["skill-1"] = None
    added = clawhub_url.prime_owner_cache({"skill-1": "snapshot-owner"})

    assert added == 0
    assert clawhub_url._OWNER_CACHE["skill-1"] is None


def test_priming_rejects_unsafe_tokens():
    """Boundary re-validation: an unsafe handle must never enter the cache.

    ``prime_owner_cache`` is public, so it cannot assume its caller validated.
    An unsafe value interpolated into a published URL is exactly the class of bug
    PR #142 / issue #139 fixed.
    """
    added = clawhub_url.prime_owner_cache(
        {
            "good-slug": "good-owner",
            "bad-slug": "../../etc/passwd",
            "dots": "...",
            "spaces": "not a handle",
        }
    )

    assert added == 1
    assert clawhub_url._OWNER_CACHE == {"good-slug": "good-owner"}


def test_priming_respects_the_cache_bound():
    """Must not grow the cache past _OWNER_CACHE_MAX."""
    original_max = clawhub_url._OWNER_CACHE_MAX
    try:
        clawhub_url._OWNER_CACHE_MAX = 5
        added = clawhub_url.prime_owner_cache({f"s{i}": f"o{i}" for i in range(50)})
        assert added <= 5
        assert len(clawhub_url._OWNER_CACHE) <= 5
    finally:
        clawhub_url._OWNER_CACHE_MAX = original_max


def test_row_owner_handle_still_wins_over_everything():
    """Regression guard: a row carrying ownerHandle needs no lookup at all.

    This path predates the fix (``row.get("ownerHandle") or resolve_owner(...)``)
    and must keep short-circuiting — priming is for rows that lack it.
    """
    adapter = ClawHubAdapter(
        fetch=lambda q: [{"slug": "s1", "displayName": "S1", "ownerHandle": "row-owner"}]
    )
    results = adapter.search("q", limit=1)

    assert "row-owner" in results[0].origin_url
    assert "s1" not in clawhub_url._OWNER_CACHE  # never consulted the cache


def test_prime_helper_is_best_effort_on_db_failure(monkeypatch):
    """A DB failure during priming must degrade, never raise into the read path."""
    from app.services import clawhub_owner_prime

    clawhub_owner_prime._reset_for_tests()

    def _boom(_db):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(
        "app.services.hub_owner_carry.load_resolved_owner_handles", _boom
    )

    # Must not raise.
    assert clawhub_owner_prime.prime_clawhub_owner_cache(object()) == 0


def test_prime_helper_ttl_skips_repeat_work(monkeypatch):
    """Priming is once-per-TTL, not once-per-request — it is on the hot path."""
    from app.services import clawhub_owner_prime

    clawhub_owner_prime._reset_for_tests()
    load_calls: list[int] = []

    def _load(_db):
        load_calls.append(1)
        return {"a-slug": "an-owner"}

    monkeypatch.setattr(
        "app.services.hub_owner_carry.load_resolved_owner_handles", _load
    )

    first = clawhub_owner_prime.prime_clawhub_owner_cache(object())
    second = clawhub_owner_prime.prime_clawhub_owner_cache(object())

    assert first == 1
    assert second == 0, "second call inside the TTL window should skip the query"
    assert len(load_calls) == 1, "the DB was queried twice inside one TTL window"
