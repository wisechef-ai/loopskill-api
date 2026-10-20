"""Tests for the /api/marketing/snapshot payload TTL cache (perf_1020).

perf_0905 already memoized the code-derived probes (_live_rest_paths /
_live_mcp_tool_names); this file pins the payload cache added on top:

1. Warm HTTP-route calls are served from cache — the pure compute runs
   exactly once. Live-overlay semantics of the PURE function are pinned by
   test_marketing_snapshot_live_overlay.py and stay untouched.
2. TTL expiry re-derives from the live DB — drift-proofing still converges.
3. clear_marketing_caches() forces immediate re-derivation (ops/watchdog hook).
4. WR_MARKETING_CACHE_TTL=0 disables caching entirely (escape hatch).
5. Mutating a returned payload cannot poison the cache.
"""
from __future__ import annotations

import time

import pytest

from app import marketing_routes as mr
from app.marketing_routes import (
    clear_marketing_caches,
    marketing_snapshot_endpoint,
)
from tests.conftest import make_skill


@pytest.fixture(autouse=True)
def _restore_default_caches():
    """Rebuild the cache with the default TTL after any test that patched it."""
    yield
    clear_marketing_caches()
    mr._build_marketing_caches()


def test_route_serves_warm_calls_from_cache(db_session, monkeypatch) -> None:
    computes = {"n": 0}
    real = mr.marketing_snapshot

    def counting(db):
        computes["n"] += 1
        return real(db)

    monkeypatch.setattr(mr, "marketing_snapshot", counting)
    clear_marketing_caches()

    first = marketing_snapshot_endpoint(db_session)
    second = marketing_snapshot_endpoint(db_session)
    third = marketing_snapshot_endpoint(db_session)

    assert computes["n"] == 1, "warm calls must not re-run the pure compute"
    assert first == second == third


def test_pure_marketing_snapshot_stays_uncached(db_session) -> None:
    """Direct calls bypass the TTL cache — live-overlay semantics preserved."""
    clear_marketing_caches()
    base_payload = marketing_snapshot_endpoint(db_session)  # fill route cache
    base = base_payload["counts"]["pro_skills"]

    make_skill(db_session, slug="pure-probe", tier="pro")
    db_session.flush()

    # HTTP route: still the cached (pre-seed) payload within TTL…
    cached = marketing_snapshot_endpoint(db_session)
    assert cached["counts"]["pro_skills"] == base
    # …but the pure function must see the freshly seeded row.
    live = mr.marketing_snapshot(db_session)
    assert live["counts"]["pro_skills"] == base + 1


def test_clear_forces_rederivation(db_session, monkeypatch) -> None:
    computes = {"n": 0}
    real = mr.marketing_snapshot

    def counting(db):
        computes["n"] += 1
        return real(db)

    monkeypatch.setattr(mr, "marketing_snapshot", counting)
    clear_marketing_caches()

    marketing_snapshot_endpoint(db_session)
    clear_marketing_caches()
    marketing_snapshot_endpoint(db_session)

    assert computes["n"] == 2, "clear_marketing_caches must force a recompute"


def test_ttl_expiry_rederives_from_db(db_session, monkeypatch) -> None:
    """After the TTL elapses, the route re-derives — drift-proofing converges."""
    monkeypatch.setenv("WR_MARKETING_CACHE_TTL", "0.05")
    mr._build_marketing_caches()

    before = marketing_snapshot_endpoint(db_session)
    make_skill(db_session, slug="expiry-probe", tier="pro")
    db_session.flush()
    time.sleep(0.1)  # > TTL

    after = marketing_snapshot_endpoint(db_session)
    assert after["counts"]["pro_skills"] == before["counts"]["pro_skills"] + 1, (
        "expired cache must re-derive from the live DB"
    )


def test_ttl_zero_disables_cache(db_session, monkeypatch) -> None:
    computes = {"n": 0}
    real = mr.marketing_snapshot

    def counting(db):
        computes["n"] += 1
        return real(db)

    monkeypatch.setenv("WR_MARKETING_CACHE_TTL", "0")
    mr._build_marketing_caches()
    monkeypatch.setattr(mr, "marketing_snapshot", counting)

    marketing_snapshot_endpoint(db_session)
    marketing_snapshot_endpoint(db_session)

    assert computes["n"] == 2, "TTL=0 must make every call recompute"


def test_response_mutation_cannot_poison_cache(db_session) -> None:
    clear_marketing_caches()
    first = marketing_snapshot_endpoint(db_session)

    first["counts"]["pro_skills"] = 999_999
    first["tiers"]["pro"]["bullets"].append("poisoned bullet")

    second = marketing_snapshot_endpoint(db_session)
    assert second["counts"]["pro_skills"] != 999_999
    assert "poisoned bullet" not in second["tiers"]["pro"]["bullets"]


def test_default_ttl_is_300s(monkeypatch) -> None:
    monkeypatch.delenv("WR_MARKETING_CACHE_TTL", raising=False)
    assert mr._marketing_cache_ttl_from_env() == 300.0
    monkeypatch.setenv("WR_MARKETING_CACHE_TTL", "garbage")
    assert mr._marketing_cache_ttl_from_env() == 300.0, "bad env falls back to default"
