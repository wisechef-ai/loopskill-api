"""Shared (Redis-backed L2) metasearch cache — unisearch_0709 P1.

The P1 problem, stated plainly: the metasearch SWR cache was per-worker, so a
result computed by worker A was invisible to worker B. The MCP search path (P2)
reads the cache ONLY — it never fans out — so on a per-worker cache it would
answer "cold" for a query the REST route had just warmed on another worker.

These tests drive the fix: an L2 Redis tier behind the SAME ``get``/``put``
interface, with honest freshness states (``fresh`` / ``stale`` / ``miss`` /
``degraded``) and graceful degradation when Redis is unreachable.

Redis is faked in-process (``_FakeRedis``) — fakeredis is NOT a dependency of
this repo and P1 is explicitly forbidden from adding one. The fake implements
only what the L2 uses: GET / SET(ex) / INCR / DELETE / SCAN and the single
compare-and-set EVAL script. Its ``eval`` asserts the script text matches
``metasearch_cache_l2._CAS_SCRIPT`` so the Python emulation cannot silently
drift from the Lua that actually runs in production.
"""

from __future__ import annotations

import json
import time

import pytest

from app.services import metasearch_cache_l2 as l2
from app.services.metasearch_cache import CacheEntry, HotQueryCache
from app.services.metasearch_cache_l2 import RedisL2


# ── Test doubles ──────────────────────────────────────────────────────────────


class _FakeRedis:
    """Minimal single-process Redis stand-in (string ops + the CAS script)."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, float | None]] = {}
        self.ops: list[str] = []

    # -- plumbing --
    def _live(self, key: str) -> str | None:
        found = self.store.get(key)
        if found is None:
            return None
        value, expires_at = found
        if expires_at is not None and time.time() > expires_at:
            self.store.pop(key, None)
            return None
        return value

    def ttl(self, key: str) -> int:
        found = self.store.get(key)
        if found is None or found[1] is None:
            return -1
        return int(round(found[1] - time.time()))

    # -- redis surface used by RedisL2 --
    def get(self, key: str) -> str | None:
        self.ops.append("get")
        return self._live(key)

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.ops.append("set")
        self.store[key] = (value, time.time() + ex if ex else None)
        return True

    def incr(self, key: str) -> int:
        self.ops.append("incr")
        nxt = int(self._live(key) or 0) + 1
        self.store[key] = (str(nxt), None)
        return nxt

    def delete(self, *keys: str) -> int:
        self.ops.append("delete")
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    def scan_iter(self, match: str | None = None, count: int | None = None):
        self.ops.append("scan_iter")
        prefix = (match or "*").rstrip("*")
        return [k for k in list(self.store) if k.startswith(prefix)]

    def eval(self, script: str, numkeys: int, *args: str):
        """Python emulation of the module's ONE Lua script (see module docstring)."""
        assert script == l2._CAS_SCRIPT, "CAS script drifted from the fake's emulation"
        assert numkeys == 1
        self.ops.append("eval")
        key, payload, seq, ttl, expected = args
        seq_i, ttl_i = int(seq), int(ttl)
        current = self._live(key)
        if current is not None:
            stored_seq = int(json.loads(current).get("seq", 0))
            if expected != "":
                if stored_seq != int(expected):
                    return 0
            elif stored_seq >= seq_i:
                return 0
        elif expected != "":
            return 0  # entry gone → a CAS refresh has nothing to compare against
        self.store[key] = (payload, time.time() + ttl_i)
        return 1


class _DownRedis:
    """Every call raises — models Redis unreachable mid-flight."""

    def __getattr__(self, name):
        def _boom(*_a, **_kw):
            raise ConnectionError(f"redis down ({name})")

        return _boom


def _worker(shared: _FakeRedis | None, **kw) -> HotQueryCache:
    """A cache instance standing in for one uvicorn worker process."""
    params = {"ttl_s": 60.0, "stale_grace_s": 120.0, **kw}
    factory = (lambda: shared) if shared is not None else (lambda: None)
    return HotQueryCache(l2=RedisL2(client_factory=factory), **params)


SRC = ("skills-sh", "clawhub")


# ── 1. Two workers, one Redis: A computes, B reads it fresh with ZERO fan-out ──


def test_second_worker_reads_first_workers_result_fresh(monkeypatch):
    """The P1 acceptance test: A ``put()``s, B ``get_entry()``s it FRESH.

    B performs zero upstream work — ``fan_out`` is monkeypatched to explode, so
    any cold fan-out on the read path fails the test rather than silently
    costing an upstream call.
    """
    import app.services.metasearch_fanout as fanout_mod

    shared = _FakeRedis()
    worker_a = _worker(shared)
    worker_b = _worker(shared)

    worker_a.put("Browser  Automation", SRC, [{"slug": "graft", "title": "Graft"}], sources_ok=["skills-sh"])

    def _explode(*_a, **_kw):  # pragma: no cover - must never run
        raise AssertionError("the cache-only reader fanned out upstream")

    monkeypatch.setattr(fanout_mod, "fan_out", _explode)

    lookup = worker_b.get_entry("browser automation", tuple(reversed(SRC)))
    assert lookup.state == "fresh"
    assert lookup.entry is not None
    assert lookup.entry.skills == [{"slug": "graft", "title": "Graft"}]
    assert lookup.entry.sources_ok == ["skills-sh"]
    # Tuple-shaped for the existing callers, attribute-shaped for the new ones.
    entry, state = lookup
    assert (entry, state) == (lookup.entry, lookup.state)


def test_shared_entry_uses_absolute_epoch_not_monotonic():
    """``computed_at`` crosses a process boundary — it MUST be unix epoch.

    ``time.monotonic()`` is per-process; a monotonic stamp read by another
    worker is meaningless (and on a long-running host reads as ancient).
    """
    shared = _FakeRedis()
    worker_a = _worker(shared)
    worker_a.put("browser", SRC, [{"slug": "a"}])

    raw = shared.get(l2.redis_key(worker_a._key("browser", SRC)))
    payload = json.loads(raw)
    assert set(payload) >= {
        "skills",
        "sources_ok",
        "sources_degraded",
        "computed_at",
        "ttl_s",
        "stale_grace_s",
        "seq",
        "state",
    }
    assert payload["v"] == l2.PAYLOAD_VERSION
    assert abs(payload["computed_at"] - time.time()) < 30
    assert payload["computed_at"] > 1_600_000_000  # epoch seconds, not uptime


def test_redis_key_is_versioned_and_ttl_covers_the_stale_window():
    shared = _FakeRedis()
    worker_a = _worker(shared, ttl_s=30.0, stale_grace_s=60.0)
    worker_a.put("browser", SRC, [{"slug": "a"}])

    key = l2.redis_key(worker_a._key("browser", SRC))
    assert key == "loopskill:metasearch:v1:browser|clawhub,skills-sh"
    # Redis TTL = ttl_s + stale_grace_s, so the stale window is servable from L2.
    assert 85 <= shared.ttl(key) <= 90


# ── 2. Stale window: past ttl_s, inside stale_grace_s ─────────────────────────


def test_stale_window_serves_payload_with_stale_state():
    shared = _FakeRedis()
    worker_a = _worker(shared, ttl_s=0.2, stale_grace_s=60.0)
    worker_b = _worker(shared, ttl_s=0.2, stale_grace_s=60.0)
    worker_a.put("browser", SRC, [{"slug": "old"}])

    time.sleep(0.25)  # past ttl_s, well inside the grace window

    lookup = worker_b.get_entry("browser", SRC)
    assert lookup.state == "stale"
    assert lookup.entry is not None and lookup.entry.skills == [{"slug": "old"}]


def test_past_ttl_plus_grace_is_a_miss_not_a_stale_serve():
    shared = _FakeRedis()
    worker_a = _worker(shared, ttl_s=0.1, stale_grace_s=0.1)
    worker_b = _worker(shared, ttl_s=0.1, stale_grace_s=0.1)
    worker_a.put("browser", SRC, [{"slug": "old"}])

    time.sleep(0.3)
    assert worker_b.get_entry("browser", SRC).state == "miss"


# ── 3. Hard miss is a normal, fast, exception-free answer ─────────────────────


def test_hard_miss_is_fast_and_raises_nothing():
    shared = _FakeRedis()
    worker_b = _worker(shared)

    t0 = time.perf_counter()
    lookup = worker_b.get_entry("nothing-ever-cached-this", SRC)
    elapsed = time.perf_counter() - t0

    assert lookup.state == "miss"
    assert lookup.entry is None
    assert lookup.payload is None
    assert elapsed < 0.5, f"cache-only miss must be fast, took {elapsed:.3f}s"


# ── 4. Redis down → degraded, L1 still answers, never an exception ────────────


def test_redis_down_degrades_to_l1_without_raising():
    down = _DownRedis()
    worker = _worker(None)
    worker.l2 = RedisL2(client_factory=lambda: down)

    # The write cannot reach Redis — it must still land in L1 and not raise.
    seq = worker.put("browser", SRC, [{"slug": "local"}])
    assert isinstance(seq, int)

    lookup = worker.get_entry("browser", SRC)
    assert lookup.state == "degraded", "an unreachable shared tier must be honest"
    assert lookup.entry is not None and lookup.entry.skills == [{"slug": "local"}]


def test_redis_backoff_window_is_degraded_not_empty_and_fresh():
    """``get_redis()`` returns None for 30s after a failure — that window must
    surface as ``degraded``, never as an empty-but-fresh answer."""
    worker = _worker(None)
    worker.l2 = RedisL2(client_factory=lambda: None)  # backoff window: no client

    lookup = worker.get_entry("browser", SRC)
    assert lookup.state == "degraded"
    assert lookup.entry is None


def test_degraded_reader_never_blocks_on_upstream(monkeypatch):
    import app.services.metasearch_fanout as fanout_mod

    monkeypatch.setattr(fanout_mod, "fan_out", lambda *a, **k: pytest.fail("reader fanned out"))
    worker = _worker(None)
    worker.l2 = RedisL2(client_factory=lambda: _DownRedis())

    t0 = time.perf_counter()
    assert worker.get_entry("browser", SRC).state == "degraded"
    assert time.perf_counter() - t0 < 0.5


def test_redis_recovers_after_an_outage():
    shared = _FakeRedis()
    flaky = {"down": True}
    worker = _worker(None)
    worker.l2 = RedisL2(client_factory=lambda: _DownRedis() if flaky["down"] else shared)

    worker.put("browser", SRC, [{"slug": "local"}])
    assert worker.get_entry("browser", SRC).state == "degraded"

    flaky["down"] = False
    worker.put("browser", SRC, [{"slug": "local2"}])
    lookup = worker.get_entry("browser", SRC)
    assert lookup.state == "fresh"
    assert lookup.entry.skills == [{"slug": "local2"}]


# ── 5. Shared seq + CAS: an older-seq write never clobbers a newer entry ──────


def test_seq_comes_from_a_shared_redis_incr():
    shared = _FakeRedis()
    worker_a = _worker(shared)
    worker_b = _worker(shared)

    seq_a = worker_a.put("q1", SRC, [{"slug": "a"}])
    seq_b = worker_b.put("q2", SRC, [{"slug": "b"}])
    seq_a2 = worker_a.put("q3", SRC, [{"slug": "c"}])

    assert seq_a < seq_b < seq_a2, "seq must be a single shared sequence, not per-process"


def test_older_seq_refresh_does_not_clobber_newer_entry():
    """Worker A's slow stale-refresh must lose to worker B's newer write."""
    shared = _FakeRedis()
    worker_a = _worker(shared)
    worker_b = _worker(shared)

    seq_old = worker_a.put("browser", SRC, [{"slug": "old"}])
    # B recomputes and stores a NEWER entry (higher shared seq).
    worker_b.put("browser", SRC, [{"slug": "new"}])

    # A's background refresh finally finishes; it started from ``seq_old``.
    landed = worker_a.put_if_current("browser", SRC, [{"slug": "stale-refresh"}], expected_seq=seq_old)
    assert landed is False

    # The shared tier still holds B's newer entry, and every worker that reads
    # it — B, and a cold worker C — gets B's value, not A's stale refresh.
    stored = json.loads(shared.get(l2.redis_key(worker_a._key("browser", SRC))))
    assert stored["skills"] == [{"slug": "new"}], "older write clobbered a newer entry"
    for worker in (worker_b, _worker(shared)):
        entry = worker.get_entry("browser", SRC).entry
        assert entry is not None and entry.skills == [{"slug": "new"}]


def test_fresh_l1_hit_short_circuits_before_redis():
    """L1 sits IN FRONT of L2: a within-TTL local hit costs no Redis round-trip.

    The cost of that shield is bounded and deliberate — two workers can hold
    different-but-both-fresh entries for at most one TTL window.
    """
    shared = _FakeRedis()
    worker = _worker(shared)
    worker.put("browser", SRC, [{"slug": "a"}])

    shared.ops.clear()
    assert worker.get_entry("browser", SRC).state == "fresh"
    assert shared.ops == [], f"a fresh L1 hit must not touch Redis, did: {shared.ops}"


def test_cas_refresh_lands_when_the_entry_is_unchanged():
    shared = _FakeRedis()
    worker_a = _worker(shared)
    worker_b = _worker(shared)

    seq0 = worker_a.put("browser", SRC, [{"slug": "old"}])
    assert worker_a.put_if_current("browser", SRC, [{"slug": "refreshed"}], expected_seq=seq0) is True
    assert worker_b.get_entry("browser", SRC).entry.skills == [{"slug": "refreshed"}]


def test_cas_refresh_fails_when_the_entry_is_gone():
    shared = _FakeRedis()
    worker_a = _worker(shared)
    worker_a.put("browser", SRC, [{"slug": "old"}])
    worker_a.invalidate()
    assert worker_a.put_if_current("browser", SRC, [{"slug": "x"}], expected_seq=1) is False


def test_invalidate_clears_the_shared_tier_too():
    shared = _FakeRedis()
    worker_a = _worker(shared)
    worker_b = _worker(shared)
    worker_a.put("browser", SRC, [{"slug": "a"}])
    worker_a.invalidate()
    assert worker_b.get_entry("browser", SRC).state == "miss"


# ── 6. put() sanitizes, field-caps and version-tags before it shares ──────────


def test_put_caps_oversized_rows_and_never_stores_them_raw():
    shared = _FakeRedis()
    worker_a = _worker(shared)
    worker_b = _worker(shared)

    hostile = {
        "slug": "evil",
        "title": "T" * 50_000,  # oversized string
        "description": "x\x00y",  # NUL injection
        "tags": [f"t{i}" for i in range(500)],  # oversized list
        **{f"junk{i}": i for i in range(200)},  # field-count blowup
    }
    worker_a.put("browser", SRC, [hostile])

    raw = shared.get(l2.redis_key(worker_a._key("browser", SRC)))
    assert "T" * 50_000 not in raw
    assert "\x00" not in raw

    row = worker_b.get_entry("browser", SRC).entry.skills[0]
    assert row["slug"] == "evil"
    assert len(row["title"]) <= l2.MAX_STR_LEN
    assert len(row["tags"]) <= l2.MAX_LIST_LEN
    assert len(row) <= l2.MAX_FIELDS


def test_put_rejects_malformed_rows():
    shared = _FakeRedis()
    worker_a = _worker(shared)
    worker_a.put("browser", SRC, ["not-a-dict", None, 42, {"slug": "ok"}])
    entry = _worker(shared).get_entry("browser", SRC).entry
    assert entry is not None and entry.skills == [{"slug": "ok"}]


def test_put_caps_the_row_count():
    shared = _FakeRedis()
    worker_a = _worker(shared)
    worker_a.put("browser", SRC, [{"slug": f"s{i}"} for i in range(l2.MAX_ROWS + 50)])
    assert len(_worker(shared).get_entry("browser", SRC).entry.skills) == l2.MAX_ROWS


def test_unparseable_or_wrong_version_payload_is_a_miss_not_a_crash():
    shared = _FakeRedis()
    worker_b = _worker(shared)
    key = l2.redis_key(worker_b._key("browser", SRC))

    for poison in ("}{not json", json.dumps({"v": 99, "skills": []}), json.dumps({"v": 1, "skills": "nope"})):
        shared.set(key, poison)
        assert worker_b.get_entry("browser", SRC).state == "miss"


def test_l1_lru_is_capped_at_64():
    """The in-process tier stays bounded; Redis does TTL eviction, L1 does LRU."""
    assert HotQueryCache().max_entries == 64


def test_sanitiser_handles_nested_and_unserialisable_values():
    """The sanitiser is the fleet-wide poisoning wall — exercise its edges."""

    class _Hostile:
        def __repr__(self) -> str:  # pragma: no cover - never reached
            raise RuntimeError("boom")

    rows = l2.sanitize_skills(
        [
            {
                "slug": "a",
                "nested": {"ok": "v", "deeper": {"too": "far"}},
                "list_of_dicts": [{"x": 1}],
                "obj": _Hostile(),
                "count": 3,
                "flag": False,
                "empty": None,
                "b" * (l2.MAX_KEY_LEN + 1): "dropped-key",
                17: "non-string key",
            }
        ]
    )
    row = rows[0]
    assert row["slug"] == "a"
    assert row["nested"] == {"ok": "v"}  # one level kept, deeper nesting dropped
    assert row["list_of_dicts"] == []  # dicts inside a list are one level too deep
    assert "obj" not in row  # unserialisable → dropped, never stringified into the payload
    assert (row["count"], row["flag"], row["empty"]) == (3, False, None)
    assert "b" * (l2.MAX_KEY_LEN + 1) not in row
    assert 17 not in row
    # The whole thing must round-trip through JSON — that is the point.
    json.dumps(rows)


def test_sanitiser_rejects_non_list_input_and_caps_sources():
    assert l2.sanitize_skills({"not": "a list"}) == []
    assert l2._clean_sources("nope") == []
    assert len(l2._clean_sources([f"s{i}" for i in range(100)])) == l2.MAX_SOURCES
    assert l2._clean_sources(["ok", 7, None]) == ["ok"]


def test_encode_payload_sheds_rows_until_it_fits_and_gives_up_honestly(monkeypatch):
    monkeypatch.setattr(l2, "MAX_PAYLOAD_BYTES", 20_000)
    huge = [{"slug": f"s{i}", "body": "x" * (l2.MAX_STR_LEN - 1)} for i in range(l2.MAX_ROWS)]
    encoded = l2.encode_payload(
        skills=huge,
        sources_ok=[],
        sources_degraded=[],
        computed_at=time.time(),
        ttl_s=60,
        stale_grace_s=60,
        seq=1,
    )
    assert encoded is not None
    assert len(encoded.encode("utf-8")) <= l2.MAX_PAYLOAD_BYTES
    assert 0 < len(json.loads(encoded)["skills"]) < l2.MAX_ROWS  # tail shed, head kept

    # A single row that cannot fit at all is not written — better no shared entry
    # than a truncated one masquerading as a complete result.
    monkeypatch.setattr(l2, "MAX_PAYLOAD_BYTES", 10)
    assert (
        l2.encode_payload(
            skills=[{"slug": "s", "body": "x" * l2.MAX_STR_LEN}],
            sources_ok=[],
            sources_degraded=[],
            computed_at=time.time(),
            ttl_s=60,
            stale_grace_s=60,
            seq=1,
        )
        is None
    )


def test_l2_never_raises_when_the_client_itself_explodes():
    """Every RedisL2 surface reports (reachable=False) instead of propagating."""

    def _bad_factory():
        raise RuntimeError("cannot build a client")

    tier = RedisL2(client_factory=_bad_factory)
    assert tier.next_seq() == (False, None)
    assert tier.read("k") == (False, None)
    assert tier.write("k", "{}", seq=1, ttl_s=10) == (False, False)
    assert tier.drop_matching() is False

    down = RedisL2(client_factory=lambda: _DownRedis())
    assert down.next_seq() == (False, None)
    assert down.read("k") == (False, None)
    assert down.write("k", "{}", seq=1, ttl_s=10) == (False, False)
    assert down.drop_matching() is False
    # Nothing to write is not a failure — it is simply not shared.
    assert down.write("k", None, seq=1, ttl_s=10) == (True, False)


def test_decode_payload_rejects_junk_without_raising():
    assert l2.decode_payload(None) is None
    assert l2.decode_payload("not json") is None
    assert l2.decode_payload(json.dumps(["a", "list"])) is None
    assert l2.decode_payload(json.dumps({"v": 1, "skills": [], "computed_at": "nope"})) is None


# ── 7. The REST route still works and now populates the SHARED cache ──────────


def test_rest_route_populates_the_shared_cache_for_other_workers(client, db_session, monkeypatch):
    """End-to-end: the metasearch route warms the shared tier, and a SECOND
    worker (a fresh cache instance) serves that query from Redis with no
    fan-out. This is the two-worker smoke test the sprint gates on."""
    from tests.test_metasearch_route import _fake_fanout

    _fake_fanout(monkeypatch, {"browse-sh": [{"slug": "s", "name": "S", "title": "S"}]})

    from app.services.metasearch_cache import get_cache

    shared = _FakeRedis()
    route_cache = get_cache()
    route_cache.invalidate()
    monkeypatch.setattr(route_cache, "l2", RedisL2(client_factory=lambda: shared))

    resp = client.get("/api/skills/metasearch?q=browser")
    assert resp.status_code == 200
    assert resp.json()["cache"]["cache_hit"] is False

    worker_b = _worker(shared, ttl_s=route_cache.ttl_s, stale_grace_s=route_cache.stale_grace_s)
    lookup = worker_b.get_entry("browser", tuple(sorted(shared_sources())))
    assert lookup.state == "fresh"
    assert [s["slug"] for s in lookup.entry.skills] == [s["slug"] for s in resp.json()["skills"]]

    route_cache.invalidate()


def shared_sources() -> tuple[str, ...]:
    from app.metasearch_routes import DEFAULT_FANOUT_SOURCES

    return tuple(DEFAULT_FANOUT_SOURCES)


# ── 8. Backward compatibility of the existing interface ──────────────────────


def test_existing_l1_only_interface_is_unchanged():
    """No l2 injected → today's pure in-process behaviour, states and all."""
    c = HotQueryCache(ttl_s=60)
    seq = c.put("browser", ("skills-sh",), [{"slug": "a"}], sources_ok=["skills-sh"])
    assert isinstance(seq, int)
    assert c.get("browser", ("skills-sh",)).skills == [{"slug": "a"}]
    entry, state = c.get_entry("browser", ("skills-sh",))
    assert state == "fresh" and entry is not None
    assert c.get_entry("never-cached", ("skills-sh",)).state == "miss"


def test_cache_entry_epoch_helpers():
    e = CacheEntry(
        skills=[],
        sources_ok=["a"],
        sources_degraded=[],
        sources_failed=[],
        computed_at=time.time() - 5.0,
        ttl_s=1.0,
        stale_grace_s=100.0,
    )
    assert e.fresh is False and e.stale is True and e.expired is False
    assert 4.5 <= e.age_s <= 6.5
