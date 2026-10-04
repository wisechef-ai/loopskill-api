"""fed1007 R3 SHOULD — end-to-end coverage of the shared fan-out budget.

PR #378 made a degraded result (more than half the sources missing) live 30 s,
and put REST stale-while-revalidate refreshes and MCP warms on ONE process-wide
budget (``metasearch_cache_swr._REFRESH_SLOTS``; ``mcp_federated_search
._warm_slots`` is the same object). The tests in ``test_fed1007_degraded_ttl.py``
pin those pieces with direct unit calls (``_maybe_refresh`` on a synthetic key,
``put_if_current`` by hand). These drive the REAL paths instead:

1. Degraded entries written by ``get_or_compute``, aged past 30 s on a clock,
   re-read through ``get_or_compute``: no more than MAX_BACKGROUND_REFRESHES
   refreshes run at once, every read is still served at once, and a refused key
   is refreshed by its next read once a slot frees.
2. While REST refreshes hold every slot, ``federated_append`` on a cold query
   answers from the local index WITHOUT starting a fan-out (the real
   ``_warm_query`` → ``metasearch_compute.warm`` → compute chain is left in
   place; only the upstream compute itself is replaced, and it fails the test).
3. Two cache instances (two workers) on one fake Redis: worker B reads worker
   A's degraded entry with the 30 s TTL, refreshes it in the background once
   healthy, and the compare-and-set in Redis stores it with the full TTL —
   which worker A then reads, and which A's own late refresh cannot clobber.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app.services import mcp_federated_search as mfs
from app.services import metasearch_cache as mc
from app.services import metasearch_cache_swr as swr
from app.services.metasearch_cache import DEGRADED_TTL_S, get_cache
from app.services.metasearch_cache_l2 import redis_key
from tests.test_unisearch_p1_shared_cache import _FakeRedis, _worker

OK14 = [f"s{i}" for i in range(14)]
COLD_OK, COLD_DEGRADED = OK14[:5], OK14[5:]  # 9/14 missing: the prod cold start
N_KEYS = swr.MAX_BACKGROUND_REFRESHES * 2


class _Clock:
    """A settable epoch clock swapped in for ``metasearch_cache.time``.

    Entry age is ``time.time() - computed_at`` in that module only, so moving
    this clock ages every entry — in L1 and in what other workers hydrate from
    Redis — without sleeping 30 s or poking ``computed_at`` by hand.
    """

    def __init__(self) -> None:
        self.now = time.time()

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(mc, "time", SimpleNamespace(time=fake.time))
    return fake


@pytest.fixture
def cache():
    """The REAL module singleton — the instance REST and MCP both use — in
    L1-only mode so the test never touches a live Redis."""
    singleton = get_cache()
    original = (singleton.l2, singleton.ttl_s)
    singleton.l2 = None
    singleton.ttl_s = 300.0
    singleton._l2_down = False
    singleton.invalidate(None)
    yield singleton
    singleton.invalidate(None)
    singleton._l2_down = False
    singleton.l2, singleton.ttl_s = original


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _BlockingCompute:
    """A healthy upstream compute that blocks until released, recording how many
    run at once and for which queries."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.started: list[str] = []

    def for_query(self, query: str):
        def _compute():
            with self.lock:
                self.running += 1
                self.peak = max(self.peak, self.running)
                self.started.append(query)
            try:
                self.gate.wait(5)
            finally:
                with self.lock:
                    self.running -= 1
            return [{"slug": f"{query}-fresh"}], OK14, []

        return _compute


def _sources() -> tuple[str, ...]:
    return mfs.federated_sources()


def _populate_degraded(cache, queries: list[str]) -> None:
    """Hard-miss each query through ``get_or_compute`` with a cold-start result."""
    for q in queries:
        entry, computed = cache.get_or_compute(
            (q, _sources()), lambda q=q: ([{"slug": f"{q}-cold"}], COLD_OK, COLD_DEGRADED)
        )
        assert computed and entry is not None
        assert entry.ttl_s == DEGRADED_TTL_S, "a 9/14-missing result must get the degraded TTL"


def _saturate(cache, clock: _Clock, blocker: _BlockingCompute) -> list[str]:
    """Write N_KEYS degraded entries, age them past 30 s, and re-read each one
    through ``get_or_compute`` with a blocking refresh. Returns the queries."""
    queries = [f"fed1007 e2e {i}" for i in range(N_KEYS)]
    _populate_degraded(cache, queries)
    clock.now += DEGRADED_TTL_S + 1  # past the degraded TTL, far inside the full one

    for q in queries:
        t0 = time.monotonic()
        entry, computed = cache.get_or_compute((q, _sources()), blocker.for_query(q))
        assert time.monotonic() - t0 < 0.5, "a stale read must be served at once, never wait on a slot"
        assert computed is False and entry is not None and entry.stale
        assert entry.skills == [{"slug": f"{q}-cold"}], "the stale entry is what gets served"
    assert _wait_until(lambda: len(blocker.started) >= swr.MAX_BACKGROUND_REFRESHES, 2.0)
    time.sleep(0.1)  # let any over-budget refresh show itself before we count
    return queries


def _drain(cache, blocker: _BlockingCompute) -> None:
    blocker.gate.set()
    assert _wait_until(lambda: not cache._refreshing), "background refreshes must finish"
    assert _wait_until(lambda: blocker.running == 0)


# ── 1. N > budget aged degraded entries: at most MAX refreshes at once ───────


def test_aged_degraded_entries_refresh_within_the_shared_budget(cache, clock):
    blocker = _BlockingCompute()
    try:
        queries = _saturate(cache, clock, blocker)
        assert blocker.peak <= swr.MAX_BACKGROUND_REFRESHES, f"{blocker.peak} fan-outs ran at once"
        assert len(blocker.started) == swr.MAX_BACKGROUND_REFRESHES, (
            "every slot must be used: an aged degraded entry has to trigger its refresh"
        )
        refused = [q for q in queries if q not in blocker.started]
        assert len(refused) == N_KEYS - swr.MAX_BACKGROUND_REFRESHES
        assert len(cache._refreshing) == swr.MAX_BACKGROUND_REFRESHES, "a refused key is not left marked"
    finally:
        _drain(cache, blocker)

    # The refreshes that ran stored a healthy result with the FULL TTL.
    for q in blocker.started:
        entry = cache.get_entry(q, _sources(), _count=False).entry
        assert entry is not None and entry.fresh and entry.ttl_s == cache.ttl_s
        assert entry.skills == [{"slug": f"{q}-fresh"}]

    # A refused key is retried by its next read, inside the same budget.
    second = _BlockingCompute()
    try:
        for q in refused:
            entry, computed = cache.get_or_compute((q, _sources()), second.for_query(q))
            assert computed is False and entry.stale
        assert _wait_until(lambda: len(second.started) == len(refused), 2.0)
        assert second.peak <= swr.MAX_BACKGROUND_REFRESHES
    finally:
        _drain(cache, second)
    for q in refused:
        assert cache.get_entry(q, _sources(), _count=False).entry.ttl_s == cache.ttl_s


# ── 2. MCP cold miss while REST refreshes hold every slot ────────────────────


def test_mcp_cold_miss_gets_the_local_floor_without_a_fanout_while_rest_holds_the_budget(
    cache, clock, monkeypatch
):
    import app.services.metasearch_compute as compute_mod
    from app.config import settings

    monkeypatch.setattr(settings, "MCP_FEDERATED_LIVE_BUDGET_S", 1.0)
    fanouts: list[str | None] = []
    # The real _warm_query → metasearch_compute.warm → get_or_compute chain stays
    # in place; only the upstream compute is replaced. Reaching it at all means
    # a fan-out started past the shared budget.
    monkeypatch.setattr(
        compute_mod, "build_unified_own_session", lambda q: fanouts.append(q) or ([], OK14, [])
    )
    floor_row = {
        "slug": "floor-row",
        "title": "floor row",
        "install_ref": "skills-sh:floor-row",
        "deployable": False,
        "install_path": "deep_link",
        "origin_url": "https://skills.sh/floor-row",
        "quality": "community",
    }
    floor_calls: list[str | None] = []

    def _floor(query, *, limit, exclude_slugs):
        floor_calls.append(query)
        return [floor_row]

    monkeypatch.setattr(mfs, "local_floor", _floor)

    blocker = _BlockingCompute()
    try:
        _saturate(cache, clock, blocker)
        assert blocker.running == swr.MAX_BACKGROUND_REFRESHES, "precondition: REST holds every slot"

        t0 = time.monotonic()
        rows, flag = mfs.federated_append("fed1007 never searched before")
        elapsed = time.monotonic() - t0

        # No warm job was admitted, so nothing is "warming" for this query: the
        # honest flag is ``cold`` (module docstring rule 2, and the existing
        # ``test_saturated_warm_slots_answer_cold_without_a_new_fanout``).
        assert flag == "cold"
        assert rows == [floor_row], "the local index answers while the budget is full"
        assert floor_calls == ["fed1007 never searched before"]
        assert elapsed < 1.0, f"waited {elapsed:.2f}s for a slot that was never coming"
        time.sleep(0.2)
        assert fanouts == [], "MCP started a fan-out past the shared budget"
        assert blocker.peak <= swr.MAX_BACKGROUND_REFRESHES
        assert cache.get_entry("fed1007 never searched before", _sources(), _count=False).entry is None
    finally:
        _drain(cache, blocker)

    # Once REST frees the budget, the same MCP query is served by a live fan-out.
    rows, flag = mfs.federated_append("fed1007 never searched before")
    assert fanouts == ["fed1007 never searched before"]
    assert flag == "fresh"


# ── 3. Two workers, one Redis: degraded → healthy via CAS ────────────────────


def test_degraded_entry_crosses_workers_and_recovers_to_full_ttl_via_cas(clock):
    shared = _FakeRedis()
    worker_a = _worker(shared)  # ttl_s=60, stale_grace_s=120
    worker_b = _worker(shared)
    key_parts = ("fed1007 cross worker", ("skills-sh", "clawhub"))
    rkey = redis_key(worker_a._key(*key_parts))

    entry_a, computed = worker_a.get_or_compute(
        key_parts, lambda: ([{"slug": "cold"}], COLD_OK, COLD_DEGRADED)
    )
    assert computed and entry_a.ttl_s == DEGRADED_TTL_S
    assert 145 <= shared.ttl(rkey) <= 150, "Redis key lives degraded TTL + grace"

    # Worker B never computed this key: it hydrates A's entry from Redis with
    # the degraded TTL intact — not its own 60 s default.
    lookup = worker_b.get_entry(*key_parts, _count=False)
    assert lookup.state == "fresh"
    assert lookup.entry.ttl_s == DEGRADED_TTL_S
    assert lookup.entry.seq == entry_a.seq

    # 31 s later the sources are healthy. B's read serves the stale degraded
    # entry at once and refreshes it in the background.
    clock.now += DEGRADED_TTL_S + 1
    refreshed = threading.Event()

    def _healthy():
        refreshed.set()
        return [{"slug": "healthy"}], OK14, []

    entry_b, computed = worker_b.get_or_compute(
        key_parts, lambda: pytest.fail("a stale hit must not compute in the foreground"), _healthy
    )
    assert computed is False and entry_b.stale and entry_b.skills == [{"slug": "cold"}]
    assert refreshed.wait(2), "an aged degraded entry must trigger a background refresh"
    assert _wait_until(lambda: not worker_b._refreshing)

    # The CAS landed in Redis with the full TTL...
    assert "eval" in shared.ops, "the refresh must store through the Redis compare-and-set"
    assert 175 <= shared.ttl(rkey) <= 180, "Redis key now lives the full TTL + grace"
    # ...and worker A, whose own L1 copy is the stale degraded one, reads it.
    healthy_a = worker_a.get_entry(*key_parts, _count=False)
    assert healthy_a.state == "fresh"
    assert healthy_a.entry.ttl_s == 60.0
    assert healthy_a.entry.skills == [{"slug": "healthy"}]
    assert healthy_a.entry.seq > entry_a.seq

    # The race the CAS exists for, across workers: B's next background refresh
    # starts from the healthy entry, the sources go cold again and outlast it,
    # and meanwhile worker A writes a NEWER entry. B's late result must be
    # refused in Redis, not clobber A's write.
    clock.now += 60.0 + 1
    release = threading.Event()
    late_started = threading.Event()

    def _late_cold():
        late_started.set()
        release.wait(5)
        return [{"slug": "late-cold"}], COLD_OK, COLD_DEGRADED

    try:
        served, _ = worker_b.get_or_compute(
            key_parts, lambda: pytest.fail("no foreground compute"), _late_cold
        )
        assert served.skills == [{"slug": "healthy"}] and served.stale
        assert late_started.wait(2)
        newer_seq = worker_a.put(*key_parts, [{"slug": "newer"}], sources_ok=OK14, sources_degraded=[])
    finally:
        release.set()
    assert _wait_until(lambda: not worker_b._refreshing)
    final = worker_b.get_entry(*key_parts, _count=False).entry
    assert final.seq == newer_seq, "B's refresh from a superseded seq overwrote A's newer entry"
    assert final.skills == [{"slug": "newer"}] and final.ttl_s == 60.0
