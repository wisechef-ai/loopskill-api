"""Tests for the metasearch unified normaliser + rank + dedupe (metasearch_0710 P0).

Pins the council-C5 correctness contract: percentile-within-source with a
missing-signal prior, canonical-identity dedupe (NOT origin_url), curated-wins
ties, and ClawHub non-deployable (Adam condition 2b).
"""

from __future__ import annotations

from app.services.federation import ExternalSkill, InstallPath
from app.services.metasearch import (
    MetasearchResult,
    UnifiedSkill,
    dedupe,
    merge_unified,
    rank,
    unify_curated,
    unify_external,
)


def _ext(
    source: str,
    slug: str,
    *,
    title: str = "",
    install_path: InstallPath = InstallPath.FETCH_ORIGIN,
    redistributable: bool = True,
    origin_url: str = "",
) -> ExternalSkill:
    return ExternalSkill(
        slug=slug,
        title=title or slug,
        source=source,
        install_path=install_path,
        origin_url=origin_url or f"https://{source}/{slug}",
        license=None,
        redistributable=redistributable,
        description=f"desc {slug}",
    )


# ── unify_external: popularity retention (council C5 fix) ─────────────────────


def test_unify_external_retains_skills_sh_installs():
    skill = _ext("skills-sh", "vercel-labs--agent-browser--agent-browser")
    raw = {
        "id": "vercel-labs/agent-browser/agent-browser",
        "installs": 531415,
        "source": "vercel-labs/agent-browser",
    }
    u = unify_external(skill, raw_row=raw)
    assert u.popularity == 531415, "skills.sh installs must survive normalisation (C5)"
    assert u.source == "skills-sh"
    assert u.quality == "community"


def test_unify_external_retains_clawhub_stats_downloads():
    skill = _ext("clawhub", "humanizer", install_path=InstallPath.DEEP_LINK, redistributable=False)
    raw = {"slug": "humanizer", "displayName": "Humanizer", "stats": {"downloads": 4242}}
    u = unify_external(skill, raw_row=raw)
    assert u.popularity == 4242, "clawhub stats.downloads must survive normalisation (C5)"


def test_unify_external_missing_popularity_is_none():
    u = unify_external(_ext("well-known", "acme--tool"), raw_row={"name": "tool"})
    assert u.popularity is None


# ── deployable derivation (Adam condition 2b) ────────────────────────────────


def test_clawhub_is_not_deployable_v1():
    """Condition 2b: ClawHub is searchable + ad-hoc install only, NEVER the
    'Deploy to fleet' button in v1. deep_link + not on the allow-list = False."""
    skill = _ext("clawhub", "some-skill", install_path=InstallPath.DEEP_LINK, redistributable=False)
    u = unify_external(skill, raw_row={"slug": "some-skill"})
    assert u.deployable is False


def test_skills_sh_is_deployable():
    skill = _ext("skills-sh", "owner--repo--skill")
    u = unify_external(skill, raw_row={"id": "owner/repo/skill", "installs": 10})
    assert u.deployable is True


def test_github_tap_is_deployable():
    skill = _ext("github-oss", "owner--repo", origin_url="https://github.com/owner/repo")
    u = unify_external(skill, raw_row={"stars": 5})
    assert u.deployable is True


def test_curated_is_always_deployable_and_curated_quality():
    u = unify_curated({"slug": "ruthless-mentor", "title": "Ruthless Mentor", "install_count": 99})
    assert u.deployable is True
    assert u.quality == "curated"
    assert u.popularity == 99
    assert u.canonical_id == "recipes:ruthless-mentor"


def test_non_redistributable_fetch_origin_not_deployable():
    """A fetch-origin skill whose license forbids redistribution is blocked by
    route_install → not deployable even on an allow-listed source."""
    skill = _ext("well-known", "locked", install_path=InstallPath.FETCH_ORIGIN, redistributable=False)
    u = unify_external(skill, raw_row={})
    assert u.deployable is False


# ── canonical identity + dedupe (council C5: NOT origin_url) ──────────────────


def test_canonical_id_collapses_github_skill_across_sources():
    """The same github skill discovered via skills.sh AND a github tap must share
    a canonical_id so dedupe collapses them (council C5 degenerate case)."""
    via_skills_sh = unify_external(
        _ext("skills-sh", "vercel-labs--agent-browser--agent-browser"),
        raw_row={
            "id": "vercel-labs/agent-browser/agent-browser",
            "installs": 999,
            "source": "vercel-labs/agent-browser",
        },
    )
    via_github = unify_external(
        _ext(
            "github-oss",
            "agent-browser",
            origin_url="https://github.com/vercel-labs/agent-browser/tree/main/agent-browser",
        ),
        raw_row={"stars": 5},
    )
    assert via_skills_sh.canonical_id == via_github.canonical_id, (
        f"cross-source dedupe key must match: {via_skills_sh.canonical_id} != {via_github.canonical_id}"
    )


def test_dedupe_keeps_higher_priority_source_and_max_popularity():
    """Duplicate collapses to the higher-priority source (skills-sh > github here
    is FALSE — github=20 < skills-sh... wait) — assert curated-independent rule:
    lower priority number wins, and popularity carries forward as the max."""
    a = UnifiedSkill(
        canonical_id="gh:owner/repo/skill",
        slug="s",
        title="S",
        description="",
        source="skills-sh",
        origin_url="",
        install_ref="",
        quality="community",
        deployable=True,
        install_path="fetch_origin",
        popularity=100,
    )
    b = UnifiedSkill(
        canonical_id="gh:owner/repo/skill",
        slug="s",
        title="S",
        description="",
        source="github-oss",
        origin_url="",
        install_ref="",
        quality="community",
        deployable=True,
        install_path="fetch_origin",
        popularity=None,
    )
    out = dedupe([a, b])
    assert len(out) == 1
    # github-oss priority (20) < skills-sh priority (10)? No: skills-sh=10 wins.
    assert out[0].source == "skills-sh"
    assert out[0].popularity == 100  # max carried forward


def test_dedupe_carries_max_popularity_when_lower_priority_has_the_signal():
    """github wins priority but skills.sh had the install signal → keep github row
    but inherit the max popularity so the signal isn't lost."""
    github = UnifiedSkill(
        canonical_id="gh:o/r/s",
        slug="s",
        title="S",
        description="",
        source="recipes",
        origin_url="",
        install_ref="",
        quality="curated",
        deployable=True,
        install_path="fetch_origin",
        popularity=5,
    )
    skills_sh = UnifiedSkill(
        canonical_id="gh:o/r/s",
        slug="s",
        title="S",
        description="",
        source="skills-sh",
        origin_url="",
        install_ref="",
        quality="community",
        deployable=True,
        install_path="fetch_origin",
        popularity=5000,
    )
    out = dedupe([github, skills_sh])
    assert len(out) == 1
    assert out[0].source == "recipes"  # curated priority 0 wins
    assert out[0].popularity == 5000  # but the real install signal carries


def test_dedupe_does_not_false_merge_distinct_source_scoped_slugs():
    a = unify_external(_ext("lobehub", "writer"), raw_row={})
    b = unify_external(_ext("browse-sh", "writer"), raw_row={})
    out = dedupe([a, b])
    assert len(out) == 2, "different sources with same slug must NOT merge"


# ── ranking (percentile-within-source + curated boost) ───────────────────────


def test_curated_wins_tie_over_equal_popularity_external():
    """Plan §5.4: curated always sorts above an external row of equal normalised
    popularity. Both single-item sources → 0.5 prior; curated boost breaks it."""
    curated = unify_curated({"slug": "cur", "title": "AAA Curated", "install_count": 1})
    external = unify_external(
        _ext("skills-sh", "ext--repo--x", title="ZZZ External"), raw_row={"installs": 1}
    )
    out = rank([external, curated])
    assert out[0].source == "recipes", "curated must outrank equal-popularity external"


def test_percentile_missing_signal_neutral_prior_no_crash():
    """A source with NO popularity on any row must not crash or produce an
    undefined distribution — every member gets the 0.5 prior (council C5)."""
    skills = [unify_external(_ext("well-known", f"a--{i}"), raw_row={}) for i in range(3)]
    out = rank(skills)
    assert len(out) == 3
    assert all(abs(s.rank_score - 0.5) < 1e-9 for s in out)


def test_percentile_orders_within_source_by_popularity():
    low = unify_external(_ext("skills-sh", "a--r--low"), raw_row={"installs": 1})
    high = unify_external(_ext("skills-sh", "b--r--high"), raw_row={"installs": 1000})
    mid = unify_external(_ext("skills-sh", "c--r--mid"), raw_row={"installs": 100})
    out = rank([low, high, mid])
    assert [s.slug for s in out] == ["b--r--high", "c--r--mid", "a--r--low"]


def test_single_item_source_gets_neutral_not_extreme_percentile():
    """Council C5 degenerate case: a one-result source must get 0.5, not a
    top/bottom extreme that would unfairly dominate or sink it."""
    solo = unify_external(_ext("skills-sh", "solo--r--x"), raw_row={"installs": 7})
    out = rank([solo])
    assert abs(out[0].rank_score - 0.5) < 1e-9


# ── merge_unified: the intact seam (one ranked list) ─────────────────────────


def test_merge_unified_returns_one_ranked_list_no_namespace_split():
    curated = [unify_curated({"slug": "cur", "title": "Curated", "install_count": 10})]
    external = [
        unify_external(_ext("skills-sh", "e--r--pop", title="Popular"), raw_row={"installs": 9999}),
        unify_external(
            _ext("clawhub", "cl", title="Claw", install_path=InstallPath.DEEP_LINK, redistributable=False),
            raw_row={"stats": {"downloads": 3}},
        ),
    ]
    result = merge_unified(curated, external, sources_ok=["recipes", "skills-sh", "clawhub"])
    assert isinstance(result, MetasearchResult)
    d = result.to_dict()
    assert "skills" in d and isinstance(d["skills"], list)
    assert d["result_count"] == 3
    assert d["source_count"] == 3
    # one flat list — no 'internal'/'external' split keys (the deleted wall)
    assert "internal" not in d and "external" not in d
    # clawhub row present + searchable but NOT deployable
    claw = next(s for s in d["skills"] if s["source"] == "clawhub")
    assert claw["deployable"] is False
    # curated carries the quality chip
    cur = next(s for s in d["skills"] if s["source"] == "recipes")
    assert cur["quality"] == "curated"


def test_merge_unified_dedupes_before_ranking():
    """A github skill on skills.sh + as a github tap must render ONCE."""
    external = [
        unify_external(
            _ext("skills-sh", "owner--repo--skill"),
            raw_row={"id": "owner/repo/skill", "installs": 50, "source": "owner/repo"},
        ),
        unify_external(
            _ext("github-oss", "skill", origin_url="https://github.com/owner/repo/tree/main/skill"),
            raw_row={},
        ),
    ]
    result = merge_unified([], external)
    assert result.to_dict()["result_count"] == 1


def test_merge_unified_no_stored_count_spotify_model():
    """Adam Q2: no catalog total anywhere — only a per-query result_count +
    source_count. Assert the payload never emits a stored 'total'/'indexed'."""
    result = merge_unified([], [unify_external(_ext("skills-sh", "a--r--x"), raw_row={"installs": 1})])
    d = result.to_dict()
    assert "total" not in d and "indexed" not in d and "catalog_size" not in d
    assert d["result_count"] == 1


def test_allow_list_gates_installable_but_unlisted_source():
    """RED-PROOF anchor: a fetch-origin + redistributable skill from a source NOT
    on _FLEET_DEPLOYABLE_SOURCES must still be non-deployable. This proves the
    allow-list is independently load-bearing (not just shadowed by the deep-link
    gate). If someone adds a new installable source, it must be explicitly
    allow-listed before it can be fleet-deployed."""
    skill = _ext("some-future-source", "x", install_path=InstallPath.FETCH_ORIGIN, redistributable=True)
    u = unify_external(skill, raw_row={})
    assert u.deployable is False, "installable-but-unlisted source must NOT be deployable"


# ── council PR #74 review regressions ────────────────────────────────────────


def test_all_equal_popularity_is_neutral_not_spread():
    """Council finding 3: 3 rows with identical popularity must each get 0.5,
    not 0.0/0.5/1.0 (which let a source mint arbitrary winners via dup signals)."""
    skills = [unify_external(_ext("skills-sh", f"a--r--{n}"), raw_row={"installs": 10}) for n in "xyz"]
    out = rank(skills)
    assert {round(s.rank_score, 4) for s in out} == {0.5}, "all-equal cohort must be uniformly neutral"


def test_canonical_id_uses_raw_id_not_escaped_slug():
    """Council finding 4: a skills.sh id whose github path contains '-' must key
    off the raw unescaped id, not the --escaped slug (which is lossy)."""
    sk = _ext("skills-sh", "my--org--repo--skill")
    u = unify_external(sk, raw_row={"id": "my-org/repo/skill", "installs": 1, "source": "my-org/repo"})
    assert u.canonical_id == "gh:my-org/repo/skill"


def test_canonical_id_distinct_ids_do_not_false_merge():
    """Two DIFFERENT skills.sh ids must not collapse to one canonical id."""
    a = unify_external(
        _ext("skills-sh", "o--r--alpha"), raw_row={"id": "o/r/alpha", "installs": 1, "source": "o/r"}
    )
    b = unify_external(
        _ext("skills-sh", "o--r--beta"), raw_row={"id": "o/r/beta", "installs": 1, "source": "o/r"}
    )
    assert a.canonical_id != b.canonical_id
    assert len(dedupe([a, b])) == 2


def test_tied_non_all_equal_popularities_share_percentile():
    """Council R2: [10, 10, 100] — the two equal 10s must share one percentile,
    not get 0.0/0.5 from plain enumeration (which lets a source mint a within-tie
    winner via duplicate signals)."""
    a = unify_external(_ext("skills-sh", "a--r--1"), raw_row={"installs": 10})
    b = unify_external(_ext("skills-sh", "b--r--2"), raw_row={"installs": 10})
    c = unify_external(_ext("skills-sh", "c--r--3"), raw_row={"installs": 100})
    scored = {s.slug: round(s.rank_score, 4) for s in rank([a, b, c])}
    assert scored["a--r--1"] == scored["b--r--2"], "tied popularities must share a percentile"
    assert scored["c--r--3"] > scored["a--r--1"], "the higher popularity must still rank above the tie"
