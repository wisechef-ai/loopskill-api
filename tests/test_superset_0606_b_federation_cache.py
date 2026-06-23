"""superset_0606 Phase B — persistent federation index-cache tests.

Covers:
  - app/services/federation_cache.py : read/write/upsert + honest-count helpers
  - /api/skills/external             : cache-backed counts, zero inline walk on a
                                       cold (non-enabled) load, walked_at/stale,
                                       admin-only ?refresh=1

All offline. The cache is a real DB table (created by conftest's create_all on
the in-memory SQLite engine); adapter walks are injected via LIVE_FETCH/monkeypatch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.services import federation_cache as fcache


# ─────────────────────────── cache service layer ────────────────────────────


class TestCacheLayer:
    def test_write_then_read_roundtrip(self, db_session):
        fcache.write_source_cache(
            db_session,
            "clawhub",
            indexed_count=68_414,
            installable_count=0,
            first_page=[{"slug": "x", "title": "X"}],
            ttl_seconds=fcache.TTL_DAILY,
        )
        block = fcache.read_source_cache(db_session, "clawhub")
        assert block is not None
        assert block["indexed"] == 68_414
        assert block["installable"] == 0
        assert block["stale"] is False  # just walked
        assert block["walked_at"] is not None
        assert fcache.read_first_page(db_session, "clawhub") == [{"slug": "x", "title": "X"}]

    def test_unknown_source_returns_none(self, db_session):
        assert fcache.read_source_cache(db_session, "nope") is None
        assert fcache.read_first_page(db_session, "nope") == []

    def test_installable_clamped_to_indexed(self, db_session):
        # A walker bug reporting installable > indexed is corrected (decision #5).
        fcache.write_source_cache(db_session, "skills-sh", indexed_count=10, installable_count=999)
        block = fcache.read_source_cache(db_session, "skills-sh")
        assert block["installable"] == 10, "installable must never exceed indexed"

    def test_failed_walk_keeps_indexed_null_and_records_error(self, db_session):
        # First a good walk, then a failed one — indexed goes NULL, first_page kept.
        fcache.write_source_cache(
            db_session,
            "lobehub",
            indexed_count=505,
            installable_count=0,
            first_page=[{"slug": "keep"}],
        )
        fcache.write_source_cache(
            db_session,
            "lobehub",
            indexed_count=None,
            installable_count=None,
            last_error="upstream 503",
        )
        block = fcache.read_source_cache(db_session, "lobehub")
        assert block["indexed"] is None  # omitted from sum, never fabricated
        assert block["last_error"] == "upstream 503"
        # first_page preserved from the last good walk (degrade to stale, not empty)
        assert fcache.read_first_page(db_session, "lobehub") == [{"slug": "keep"}]

    def test_stale_after_ttl(self, db_session):
        row = fcache.write_source_cache(
            db_session,
            "browse-sh",
            indexed_count=375,
            installable_count=375,
            ttl_seconds=3600,
        )
        # Force walked_at into the past beyond the TTL.
        row.walked_at = datetime.now(timezone.utc) - timedelta(seconds=7200)
        db_session.flush()
        block = fcache.read_source_cache(db_session, "browse-sh")
        assert block["stale"] is True

    def test_sum_omits_null_sources(self, db_session):
        fcache.write_source_cache(db_session, "a", indexed_count=100, installable_count=10)
        fcache.write_source_cache(db_session, "b", indexed_count=None, installable_count=None)
        fcache.write_source_cache(db_session, "c", indexed_count=50, installable_count=5)
        blocks = fcache.read_all_cached(db_session)
        assert fcache.sum_indexed(blocks) == 150  # b (null) omitted, never counted as 0
        assert fcache.sum_installable(blocks) == 15

    def test_upsert_overwrites_same_source(self, db_session):
        fcache.write_source_cache(db_session, "hermes-hub", indexed_count=74, installable_count=74)
        fcache.write_source_cache(db_session, "hermes-hub", indexed_count=169, installable_count=169)
        block = fcache.read_source_cache(db_session, "hermes-hub")
        assert block["indexed"] == 169  # one row per source, last write wins


# ─────────────────────────── cache-backed route ─────────────────────────────


def _client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


class TestExternalRouteCacheBacked:
    def test_cold_load_reads_cache_no_inline_walk(self, db_session, monkeypatch):
        """A cold (non-enabled) load reads counts from the persistent cache and
        triggers ZERO adapter walks (decision #7)."""
        import app.services.federation_adapters as fa

        # Seed the cache as the reindex cron would.
        fcache.write_source_cache(db_session, "clawhub", indexed_count=68_414, installable_count=0)
        fcache.write_source_cache(db_session, "skills-sh", indexed_count=20_000, installable_count=20_000)

        # Tripwire: any adapter construction during a cold load is a failure.
        walk_calls = {"n": 0}
        real_get_adapter = fa.get_adapter

        def _spy_get_adapter(*a, **k):
            walk_calls["n"] += 1
            return real_get_adapter(*a, **k)

        monkeypatch.setattr("app.skill_routes.get_adapter", _spy_get_adapter, raising=False)

        client = _client(db_session, monkeypatch)
        r = client.get("/api/skills/external")  # no sources → all disabled (cold)
        assert r.status_code == 200
        body = r.json()
        assert body["per_source"]["clawhub"]["indexed"] == 68_414
        assert body["per_source"]["skills-sh"]["indexed"] == 20_000
        # Honest dual-count sums the cache, omitting null sources.
        assert body["counts"]["external_indexed"] >= 88_414
        # No adapter was constructed (zero inline walk).
        assert walk_calls["n"] == 0, "cold load must NOT walk any adapter"

    def test_per_source_carries_walked_at_and_stale(self, db_session, monkeypatch):
        fcache.write_source_cache(db_session, "browse-sh", indexed_count=375, installable_count=375)
        client = _client(db_session, monkeypatch)
        body = client.get("/api/skills/external").json()
        block = body["per_source"]["browse-sh"]
        assert block["walked_at"] is not None
        assert block["stale"] is False
        assert "indexed" in block and "installable" in block

    def test_null_source_omitted_from_sum(self, db_session, monkeypatch):
        # Seed EVERY live source so the in-memory first-boot fallback (which would
        # otherwise hit the network for uncached static catalogs) never fires —
        # this isolates the null-omission behaviour.
        from app.services.federation import LIVE_SOURCES

        for src in LIVE_SOURCES:
            fcache.write_source_cache(db_session, src, indexed_count=0, installable_count=0)
        # Now override two specific sources: one null (failed), one real.
        fcache.write_source_cache(
            db_session, "clawhub", indexed_count=None, installable_count=None, last_error="walk failed"
        )
        fcache.write_source_cache(db_session, "browse-sh", indexed_count=375, installable_count=375)
        client = _client(db_session, monkeypatch)
        body = client.get("/api/skills/external").json()
        assert body["per_source"]["clawhub"]["indexed"] is None  # never fabricated
        # Sum = browse-sh 375 + all others 0; clawhub (null) omitted, not counted as 0 or fabricated.
        assert body["counts"]["external_indexed"] == 375

    def test_refresh_requires_admin(self, db_session, monkeypatch):
        # An unauthenticated caller passing refresh=1 must NOT trigger a walk.
        client = _client(db_session, monkeypatch)
        body = client.get("/api/skills/external?refresh=1").json()
        assert body["refreshed"] is False, "refresh is admin-only; anon must be ignored"


# ─────────────────────────── reindex walker ─────────────────────────────────


class TestReindexWalker:
    def test_reindex_source_writes_cache(self, db_session, monkeypatch):
        import scripts.federation_reindex as reindex

        # Inject a deterministic fetch via the LIVE_FETCH registry entry (the
        # adapter is constructed with LIVE_FETCH.get(source) as its fetch).
        import app.services.federation_live as fl

        fl._cache.clear()
        monkeypatch.setitem(
            fl.LIVE_FETCH,
            "browse-sh",
            lambda q: [
                {"slug": f"s{i}", "name": f"s{i}", "title": f"S{i}", "description": "", "tags": []}
                for i in range(5)
            ],
        )
        # browse-sh is FETCH_ORIGIN → all 5 installable.
        report = reindex.reindex_source(db_session, "browse-sh")
        assert report["status"] == "ok"
        assert report["indexed"] == 5
        block = fcache.read_source_cache(db_session, "browse-sh")
        assert block["indexed"] == 5

    def test_reindex_failed_source_records_null(self, db_session, monkeypatch):
        import scripts.federation_reindex as reindex
        import app.services.federation_adapters as fa

        class _BoomAdapter:
            def search(self, *a, **k):
                raise RuntimeError("upstream down")

        monkeypatch.setattr(fa, "get_adapter", lambda *a, **k: _BoomAdapter())
        # NOTE: use a shallow-adapter source (lobehub). superset_0606 Phase D
        # reroutes clawhub/skills-sh through DEEP_WALKERS *before* get_adapter, so
        # those two no longer exercise the adapter-failure path (their own
        # deep-walker failure path is covered in the Phase D suite).
        report = reindex.reindex_source(db_session, "lobehub")
        assert report["status"] == "error"
        block = fcache.read_source_cache(db_session, "lobehub")
        assert block["indexed"] is None  # failed walk → NULL, omitted from sum
        assert "upstream down" in (block["last_error"] or "")

    def test_reindex_dry_run_does_not_write(self, db_session, monkeypatch):
        import scripts.federation_reindex as reindex
        import app.services.federation_live as fl

        fl._cache.clear()
        monkeypatch.setitem(
            fl.LIVE_FETCH,
            "browse-sh",
            lambda q: [{"slug": "s1", "name": "s1", "title": "S1", "description": "", "tags": []}],
        )
        reindex.reindex_source(db_session, "browse-sh", dry_run=True)
        assert fcache.read_source_cache(db_session, "browse-sh") is None  # no write
