"""fed1007 — a result most sources did not answer gets a short TTL.

Prod 2026-10-04, right after the 0.9.55 deploy: q="ASD-STE100 simplified
technical english" was computed while 9 of 14 sources were still cold, cached
with the full 300 s TTL (+600 s stale grace) and served as a 1-row result long
after every source was healthy again.
"""

from __future__ import annotations

import pytest

from app.services.metasearch_cache import DEGRADED_TTL_S, HotQueryCache

OK14 = [f"s{i}" for i in range(14)]


def _cache() -> HotQueryCache:
    return HotQueryCache(l2=None, ttl_s=300.0)


@pytest.mark.parametrize(
    ("ok", "degraded", "failed", "expected"),
    [
        (OK14[:5], OK14[5:], [], DEGRADED_TTL_S),  # 9/14 missing: prod cold start
        (OK14[:6], OK14[6:], [], DEGRADED_TTL_S),  # 8/14 = 57%
        (OK14[:7], OK14[7:], [], 300.0),  # 7/14 = 50%: not MORE than half
        (OK14[:10], OK14[10:12], OK14[12:], 300.0),  # 4/14: steady state upper end
        (OK14[:12], OK14[12:], [], 300.0),  # 2/14: steady state
        (OK14[:1], [], OK14[1:3], DEGRADED_TTL_S),  # failed counts as missing: 2/3
        ([], [], [], 300.0),  # no source accounting at all
    ],
)
def test_ttl_follows_the_share_of_missing_sources(ok, degraded, failed, expected):
    entry = _cache()._build_entry([], sources_ok=ok, sources_degraded=degraded, sources_failed=failed)
    assert entry.ttl_s == expected


def test_cold_start_entry_is_fresh_for_only_degraded_ttl():
    cache = _cache()
    cache.put("q", ("a",), [{"slug": "x"}], sources_ok=OK14[:5], sources_degraded=OK14[5:])
    entry = cache.get_entry("q", ("a",)).entry
    assert entry is not None and entry.ttl_s == DEGRADED_TTL_S


def test_a_background_refresh_that_recovers_gets_the_full_ttl():
    cache = _cache()
    seq = cache.put("q", ("a",), [], sources_ok=OK14[:5], sources_degraded=OK14[5:])
    assert cache.put_if_current(
        "q", ("a",), [{"slug": "x"}], expected_seq=seq, sources_ok=OK14, sources_degraded=[]
    )
    assert cache.get_entry("q", ("a",)).entry.ttl_s == 300.0


def test_degraded_ttl_never_exceeds_a_shorter_configured_ttl():
    cache = HotQueryCache(l2=None, ttl_s=10.0)
    entry = cache._build_entry([], sources_ok=["a"], sources_degraded=["b", "c", "d"])
    assert entry.ttl_s == 10.0


# ── fed1007: domain-hosted skills.sh ids are link-only ──────────────────────


@pytest.mark.parametrize(
    ("ident", "installable"),
    [
        ("cgoern/skills/simplified-technical-english", True),
        ("dylantarre/animation-principles/animation-principles---advanced", True),
        ("skills.volces.com/court-form-filling-pdf", False),
        ("lonely-skill", False),
    ],
)
def test_only_github_shaped_skills_sh_ids_are_installable(ident, installable):
    from app.services.federation import InstallPath
    from app.services.federation_adapters import SkillsShAdapter
    from app.services.metasearch import unify_external

    skill = SkillsShAdapter()._map({"id": ident, "name": ident.rsplit("/", 1)[-1], "source": "x"})
    assert (skill.install_path == InstallPath.FETCH_ORIGIN) is installable
    assert skill.origin_url == f"https://skills.sh/{ident}"
    assert unify_external(skill).deployable is installable
