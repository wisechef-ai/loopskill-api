"""spotify2607fix_2 (tori sp2607fix-2) — HIGH-severity perf fix for the LIVE
(non-empty-query) fan-out inside ``/api/skills/external``.

Covers ``app/services/external_fanout.py`` in isolation (no network — every
source is a monkeypatched/mocked callable, never a real sleep of 130s) plus the
route-level integration: slow source degrades at the budget, fast sources'
results still return, the degraded source is reported, total wall-clock stays
bounded, and the empty-query cached-first-page path (the ALREADY-correct
0.43s behaviour) is pinned so this fix cannot regress it.
"""

from __future__ import annotations

import time

import app.services.federation_live as fl
from app.services.external_fanout import run_external_fanout
from app.services.federation import ExternalSkill, InstallPath


def setup_function(_):
    fl._cache.clear()


def teardown_function(_):
    fl._cache.clear()


class _ClearsCache:
    """Mixin: pytest's module-level setup_function/teardown_function hooks do
    NOT fire for methods inside test classes (only for bare functions) — use
    setup_method/teardown_method so every class below actually gets a clean
    federation_live TTL cache per test, preventing cross-test cache pollution."""

    def setup_method(self, _method):
        fl._cache.clear()

    def teardown_method(self, _method):
        fl._cache.clear()


class _FakeAdapter:
    """Minimal SourceAdapter stand-in — a plain callable, no network."""

    def __init__(self, fn):
        self._fn = fn

    def search(self, query: str, limit: int = 20):
        return self._fn(query, limit)


def _skill(slug: str) -> ExternalSkill:
    return ExternalSkill(
        slug=slug,
        title=slug,
        source="test-source",
        install_path=InstallPath.DEEP_LINK,
        origin_url=f"https://example.com/{slug}",
    )


# ─────────────────────── run_external_fanout: unit level ────────────────────


class TestFanoutTimeoutBehavior(_ClearsCache):
    def test_slow_source_abandoned_at_budget_fast_source_still_returns(self):
        """(a) a slow source is abandoned at the budget, (b) the fast source's
        results still return, (c) the slow source is reported degraded."""

        def slow(_q, _limit):
            time.sleep(2.0)
            return [_skill("slow-result")]

        def fast(_q, _limit):
            return [_skill("fast-result")]

        pending = [
            ("slow-source", _FakeAdapter(slow)),
            ("fast-source", _FakeAdapter(fast)),
        ]
        t0 = time.monotonic()
        results = run_external_fanout(pending, query="docker", limit=20, per_source_deadline_s=0.3)
        elapsed = time.monotonic() - t0

        # (d) total wall-clock stays bounded — nowhere near the 2.0s sleep,
        # and nowhere near a serial 2.0+epsilon sum.
        assert elapsed < 1.0, f"fan-out must not block on the slow source, took {elapsed:.2f}s"

        assert results["fast-source"].ok is True
        assert [s.slug for s in results["fast-source"].skills] == ["fast-result"]

        assert results["slow-source"].ok is False
        assert results["slow-source"].reason == "timeout"
        assert results["slow-source"].skills == []

    def test_all_sources_healthy_returns_promptly(self):
        pending = [
            ("a", _FakeAdapter(lambda q, limit: [_skill("a1")])),
            ("b", _FakeAdapter(lambda q, limit: [_skill("b1")])),
        ]
        t0 = time.monotonic()
        results = run_external_fanout(pending, query="x", limit=20, per_source_deadline_s=2.5)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert results["a"].ok and results["b"].ok

    def test_source_error_is_degraded_not_fatal(self):
        def boom(_q, _limit):
            raise RuntimeError("upstream down")

        pending = [
            ("broken", _FakeAdapter(boom)),
            ("healthy", _FakeAdapter(lambda q, limit: [_skill("ok")])),
        ]
        results = run_external_fanout(pending, query="x", limit=20, per_source_deadline_s=2.5)
        assert results["broken"].ok is False
        assert results["broken"].reason == "fetch_error"
        assert results["healthy"].ok is True

    def test_concurrent_not_serial_two_slow_sources(self):
        """The core bug fix: N sources at ~deadline each must NOT sum. Two
        sources each sleeping past the deadline must still return in
        ~deadline, not ~2x deadline (which is what a serial walk would give)."""

        def slow(_q, _limit):
            time.sleep(1.5)
            return [_skill("late")]

        pending = [
            ("slow-1", _FakeAdapter(slow)),
            ("slow-2", _FakeAdapter(slow)),
        ]
        t0 = time.monotonic()
        results = run_external_fanout(pending, query="x", limit=20, per_source_deadline_s=0.3)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.9, f"two parallel slow sources must not sum, took {elapsed:.2f}s"
        assert results["slow-1"].reason == "timeout"
        assert results["slow-2"].reason == "timeout"

    def test_empty_pending_returns_empty_dict_instantly(self):
        results = run_external_fanout([], query="x", limit=20, per_source_deadline_s=2.5)
        assert results == {}


class TestFanoutQueryCache(_ClearsCache):
    def test_repeated_query_hits_cache_second_call_skips_adapter(self):
        calls = {"n": 0}

        def counting(_q, _limit):
            calls["n"] += 1
            return [_skill("cached-result")]

        pending = [("src", _FakeAdapter(counting))]
        r1 = run_external_fanout(pending, query="docker", limit=20, per_source_deadline_s=2.5)
        r2 = run_external_fanout(pending, query="docker", limit=20, per_source_deadline_s=2.5)
        assert calls["n"] == 1, "second identical (source, query, limit) call must be served from cache"
        assert r1["src"].ok and r2["src"].ok
        assert [s.slug for s in r2["src"].skills] == ["cached-result"]

    def test_cache_key_is_normalized_query_case_and_whitespace_insensitive(self):
        calls = {"n": 0}

        def counting(_q, _limit):
            calls["n"] += 1
            return [_skill("x")]

        pending = [("src", _FakeAdapter(counting))]
        run_external_fanout(pending, query="  Docker  ", limit=20, per_source_deadline_s=2.5)
        run_external_fanout(pending, query="docker", limit=20, per_source_deadline_s=2.5)
        assert calls["n"] == 1

    def test_different_limit_is_a_different_cache_entry(self):
        calls = {"n": 0}

        def counting(_q, limit):
            calls["n"] += 1
            return [_skill("x")]

        pending = [("src", _FakeAdapter(counting))]
        run_external_fanout(pending, query="docker", limit=20, per_source_deadline_s=2.5)
        run_external_fanout(pending, query="docker", limit=48, per_source_deadline_s=2.5)
        assert calls["n"] == 2, "different limit must be a distinct cache entry, not silently reused"


# ─────────────────────────── route-level integration ────────────────────────


class TestExternalRouteFanoutIntegration(_ClearsCache):
    """Wires a slow + a fast source into the REAL /api/skills/external route via
    LIVE_FETCH monkeypatching (no real network), proving the route-level
    behaviour end to end."""

    def test_non_empty_query_bounded_and_degrades_slow_source(self, client, monkeypatch):
        def slow_fetch(_q):
            time.sleep(4.0)
            return [{"slug": "research--slow", "title": "slow", "url": "https://h/x", "license": "MIT"}]

        monkeypatch.setattr(
            fl,
            "_load_hermes_catalog",
            lambda: [
                {
                    "slug": "research--fast",
                    "title": "fast",
                    "description": "d",
                    "url": "https://h/y",
                    "license": "MIT",
                }
            ],
        )
        # Wire THREE sources slow (well-known, lobehub, browse-sh all default to
        # no-op/empty fetches — swap them all to slow so a SERIAL walk would sum
        # to ~12s+, and each individually exceeds the default 2.5s per-source
        # deadline so all three must be reported degraded).
        monkeypatch.setitem(fl.LIVE_FETCH, "well-known", slow_fetch)
        monkeypatch.setitem(fl.LIVE_FETCH, "lobehub", slow_fetch)
        monkeypatch.setitem(fl.LIVE_FETCH, "browse-sh", slow_fetch)

        t0 = time.monotonic()
        r = client.get("/api/skills/external?sources=hermes-hub,well-known,lobehub,browse-sh&q=fast&limit=20")
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        # A SERIAL walk of 3 slow (4.0s) sources would take >=12s; concurrent
        # fan-out bounds the whole request near the per-source deadline
        # (default 2.5s) regardless of how many slow sources are queried.
        assert elapsed < 4.0, f"non-empty-query fan-out must be CONCURRENT not serial, took {elapsed:.2f}s"

        body = r.json()
        assert "degraded_sources" in body, "additive key must be present"
        for slow_src in ("well-known", "lobehub", "browse-sh"):
            assert slow_src in body["degraded_sources"]
        # hermes-hub (fast) still contributes its result despite three sibling
        # sources timing out — partial results, never total failure.
        assert body["per_source"]["hermes-hub"]["indexed"] == 1
        assert len(body["external"]) == 1
        assert body["external"][0]["slug"] == "research--fast"

    def test_empty_query_behaviour_pinned_still_fast_and_correct(self, client, db_session, monkeypatch):
        """RED-PROOF TARGET: this must NEVER regress. Empty q is currently
        0.43s in prod because it serves the persistent per-source cache and
        never touches the live fan-out at all."""
        from app.services import federation_cache as fcache

        monkeypatch.setattr(
            fl,
            "_load_hermes_catalog",
            lambda: [
                {
                    "slug": "research--x",
                    "title": "x",
                    "description": "d",
                    "url": "https://h/x",
                    "license": "MIT",
                }
            ],
        )
        fcache.write_source_cache(
            db_session,
            "hermes-hub",
            indexed_count=1,
            installable_count=1,
            first_page=[
                {
                    "slug": "research--x",
                    "title": "x",
                    "source": "hermes-hub",
                    "install_path": "fetch_origin",
                    "origin_url": "https://h/x",
                    "license": "MIT",
                    "redistributable": True,
                    "description": "d",
                }
            ],
        )

        # Even if a sibling live source would hang, empty-q must never reach
        # the fan-out at all for an already-cached source.
        def hangs_forever(_q):
            time.sleep(5.0)
            return []

        monkeypatch.setitem(fl.LIVE_FETCH, "well-known", hangs_forever)

        t0 = time.monotonic()
        r = client.get("/api/skills/external?sources=hermes-hub&limit=20")
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert elapsed < 1.0, f"empty-query must stay on the fast cached path, took {elapsed:.2f}s"
        body = r.json()
        assert len(body["external"]) == 1
        assert body["external"][0]["slug"] == "research--x"


# ── Codex adversarial-review follow-ups (2026-07-26) ────────────────────────
# The mandatory review gate returned REQUEST_CHANGES with 4 MUST-FIX. All four
# were CONFIRMED against the code and fixed. These pin them.


class TestCodexReviewFixes(_ClearsCache):
    def test_late_worker_does_not_poison_the_shared_cache(self):
        """MUST-FIX 1: a worker that misses the deadline must NOT write to the
        shared TTL cache.

        Tearing down the executor without waiting does not stop a RUNNING
        thread. Without a deadline guard the straggler eventually calls
        ``fl._cache.put()``, so the NEXT request gets its stale rows as a
        "cache hit" — and an older slow result can overwrite a newer fast one
        for the same key (last-writer-wins across overlapping requests). A slow
        upstream would poison a fast one.
        """
        from app.services.external_fanout import _cache_key

        started = []

        def _slow(_q, _lim):
            started.append(1)
            time.sleep(0.9)
            return [_skill("late-row")]

        res = run_external_fanout(
            [("slowsrc", _FakeAdapter(_slow))],
            query="q",
            limit=10,
            per_source_deadline_s=0.1,
        )
        assert res["slowsrc"].ok is False
        assert res["slowsrc"].reason in {"timeout", "saturated"}

        # Let the straggler finish and attempt its (now-forbidden) cache write.
        time.sleep(1.4)
        assert started, "precondition: the worker actually ran"
        assert fl._cache.get(_cache_key("slowsrc", "q", 10), fl._SEARCH_TTL_S) is None, (
            "a late worker wrote its stale result into the shared cache — the next "
            "request would serve it as a cache hit"
        )

    def test_fanout_uses_one_shared_bounded_pool_not_one_per_request(self):
        """MUST-FIX 2: repeated fan-outs must not accumulate executors/threads.

        A per-request executor let every timed-out request abandon up to 7 live
        threads (cancelling futures only stops work that never STARTED).
        Response bounded, resource use unbounded. Assert a single shared pool
        identity across calls, and that live thread count does not grow per call.
        """
        import threading

        from app.services import external_fanout as ef

        ef.run_external_fanout(
            [("a", _FakeAdapter(lambda q, lim: [_skill("x")]))],
            query="q1",
            limit=5,
            per_source_deadline_s=1.0,
        )
        pool_first = ef._shared_pool()
        threads_after_first = threading.active_count()

        for i in range(6):
            ef.run_external_fanout(
                [("a", _FakeAdapter(lambda q, lim: [_skill("x")]))],
                query=f"q{i}",
                limit=5,
                per_source_deadline_s=1.0,
            )

        assert ef._shared_pool() is pool_first, "a new executor was created per request"
        assert threading.active_count() <= threads_after_first + ef._MAX_WORKERS, (
            "thread count grew per request — the pool is not actually shared/bounded"
        )

    def test_force_refresh_bypasses_the_query_cache(self):
        """MUST-FIX 3: an admin ?refresh=1 must re-touch the upstream.

        Without this the route serves a TTL-cached result and then writes it to
        the PERSISTENT source cache as if freshly walked — defeating the only
        mechanism an operator has to force a real refresh.
        """
        calls = []

        def _count(_q, _lim):
            calls.append(1)
            return [_skill("row")]

        ad = _FakeAdapter(_count)
        run_external_fanout([("s", ad)], query="q", limit=5, per_source_deadline_s=2.0)
        assert len(calls) == 1

        # Same query again: served from cache, adapter NOT re-entered.
        run_external_fanout([("s", ad)], query="q", limit=5, per_source_deadline_s=2.0)
        assert len(calls) == 1, "second identical call should have hit the cache"

        # force_refresh must go back to the adapter.
        run_external_fanout([("s", ad)], query="q", limit=5, per_source_deadline_s=2.0, force_refresh=True)
        assert len(calls) == 2, "force_refresh did NOT bypass the query cache"

    def test_adapter_receives_the_same_normalized_query_used_for_the_cache_key(self):
        """SHOULD-FIX (accepted): key normalisation and execution must agree.

        The key lowercased/collapsed whitespace while the ADAPTER got the raw
        string, so '  Docker  ' and 'docker' shared a cache entry despite
        potentially producing different upstream results.
        """
        seen = []

        def _capture(q, _lim):
            seen.append(q)
            return [_skill("row")]

        run_external_fanout(
            [("s", _FakeAdapter(_capture))], query="  Docker  ", limit=5, per_source_deadline_s=2.0
        )
        assert seen == ["docker"], f"adapter got the un-normalized query: {seen!r}"
