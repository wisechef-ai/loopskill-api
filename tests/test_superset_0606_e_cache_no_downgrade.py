"""superset_0606 Phase E — cache-no-downgrade invariant for /api/skills/external.

The bug this pins (caught live 2026-06-06): an enabled-source toggle browse is
capped at ``limit`` (e.g. 50). The shipped route wrote that capped count back to
the canonical cache on every empty-query enabled search — so toggling ClawHub
(real 69k) overwrote the reindex cron's deep-walked count with 50. The portal's
own 130-page static build did exactly this, corrupting every giant's number.

Phase E fix (decision #7, enforced here):
  - The canonical count is OWNED by the reindex cron (full walk).
  - The route may write only on (a) admin force_refresh, or (b) first-boot SEED
    of a source with no cache row yet AND a non-capped result.
  - It must NEVER overwrite a larger cached indexed with a smaller capped one.
  - On a live toggle of an already-cached source, the per_source block surfaces
    the REAL cached totals, not the capped live ones.

All offline — adapter walks injected via LIVE_FETCH; cache is the real SQLite table.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services import federation_cache as fcache


def _client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _inject_fetch(monkeypatch, source: str, n_rows: int):
    """Make ``source``'s live adapter return ``n_rows`` synthetic rows on search.

    Uses a generic row shape that the clawhub/skills-sh/browse-sh adapters all
    map without error (slug/displayName/name/title/description present).
    """
    import app.services.federation_live as fl

    def _fetch(_q, _rows=n_rows):
        return [
            {
                "slug": f"{source}-s{i}",
                "displayName": f"S{i}",
                "name": f"S{i}",
                "title": f"S{i}",
                "summary": "",
                "description": "",
                "tags": [],
            }
            for i in range(_rows)
        ]

    fl._cache.clear()
    monkeypatch.setitem(fl.LIVE_FETCH, source, _fetch)


class TestCacheNoDowngrade:
    def test_capped_toggle_does_not_overwrite_deep_count(self, db_session, monkeypatch):
        """THE REGRESSION: cron seeded clawhub=69_280; a capped toggle browse
        (50 rows) must NOT overwrite it. The cache keeps the real number."""
        fcache.write_source_cache(db_session, "clawhub", indexed_count=69_280, installable_count=0)
        # A toggle browse returns a full page == limit (capped/truncated).
        _inject_fetch(monkeypatch, "clawhub", 50)

        client = _client(db_session, monkeypatch)
        body = client.get("/api/skills/external?sources=clawhub&limit=50").json()

        # The per_source block reports the REAL cached total, not the capped 50.
        assert body["per_source"]["clawhub"]["indexed"] == 69_280
        # And the cache row itself is untouched.
        assert fcache.read_source_cache(db_session, "clawhub")["indexed"] == 69_280

    def test_toggle_still_returns_live_rows_for_browsing(self, db_session, monkeypatch):
        """The fix preserves browsing: the toggle still returns the live result
        rows (so the user sees skills) — it just doesn't clobber the count."""
        fcache.write_source_cache(db_session, "clawhub", indexed_count=69_280, installable_count=0)
        _inject_fetch(monkeypatch, "clawhub", 50)
        client = _client(db_session, monkeypatch)
        body = client.get("/api/skills/external?sources=clawhub&limit=50").json()
        assert len(body["external"]) == 50  # rows still flow to the grid
        assert body["enabled_sources"] == ["clawhub"]

    def test_first_boot_seeds_uncapped_source(self, db_session, monkeypatch):
        """Before the cron's first run, a source with NO cache row may be SEEDED
        by an uncapped enabled browse (result shorter than limit = full)."""
        _inject_fetch(monkeypatch, "browse-sh", 12)  # 12 < limit 50 → not capped
        client = _client(db_session, monkeypatch)
        body = client.get("/api/skills/external?sources=browse-sh&limit=50").json()
        assert body["per_source"]["browse-sh"]["indexed"] == 12
        # Seeded into the cache for the next cold load.
        assert fcache.read_source_cache(db_session, "browse-sh")["indexed"] == 12

    def test_first_boot_does_not_seed_capped_source(self, db_session, monkeypatch):
        """A capped result on an un-cached source must NOT seed a misleading
        floor as if it were canonical — the cron will fill the real number."""
        _inject_fetch(monkeypatch, "clawhub", 50)  # capped → not canonical
        client = _client(db_session, monkeypatch)
        body = client.get("/api/skills/external?sources=clawhub&limit=50").json()
        # No cache row was written (capped result is not a canonical seed).
        assert fcache.read_source_cache(db_session, "clawhub") is None
        # The live block still reports what the browse found (honest live count).
        assert body["per_source"]["clawhub"]["indexed"] == 50

    def test_query_search_never_writes_cache(self, db_session, monkeypatch):
        """A user-filtered (q=...) search must never touch the canonical cache."""
        fcache.write_source_cache(db_session, "clawhub", indexed_count=69_280, installable_count=0)
        _inject_fetch(monkeypatch, "clawhub", 3)
        client = _client(db_session, monkeypatch)
        client.get("/api/skills/external?sources=clawhub&q=astro&limit=50")
        assert fcache.read_source_cache(db_session, "clawhub")["indexed"] == 69_280

    def test_dual_count_survives_a_toggle(self, db_session, monkeypatch):
        """End-to-end: seed the giants, toggle one, confirm the dual-count still
        sums the REAL totals (the portal-build corruption scenario)."""
        from app.services.federation import LIVE_SOURCES

        for src in LIVE_SOURCES:
            fcache.write_source_cache(db_session, src, indexed_count=0, installable_count=0)
        fcache.write_source_cache(db_session, "clawhub", indexed_count=69_280, installable_count=0)
        fcache.write_source_cache(db_session, "skills-sh", indexed_count=19_952, installable_count=None)
        _inject_fetch(monkeypatch, "clawhub", 50)

        client = _client(db_session, monkeypatch)
        body = client.get("/api/skills/external?sources=clawhub&limit=50").json()
        # Sum still reflects the deep counts, not a clobbered 50.
        assert body["counts"]["external_indexed"] >= 69_280 + 19_952
