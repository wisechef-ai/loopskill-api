"""fdeloop_0808 Phase B2 — relevance must outrank the curated/popularity boost.

## The defects, measured on live prod 2026-08-10

Phase B (PR #209) fixed recall: relevance is now applied BEFORE truncation at the
adapter layer, so the exact row survives the cut. It did NOT fix ORDER at the
merge layer, because `metasearch.rank()` never sees the query at all:

    scored.sort(key=lambda s: (-s.rank_score, _source_priority(s.source), title))

where `rank_score = popularity_percentile + (curated ? 1.0 : 0)`. A curated
first-party row therefore outranks an exact slug match for ANY query.

Observed:

    q=seo          -> ['hundred-million-offers', ...seo-audit, ...programmatic-seo]
                      the row slugged exactly `seo` is present but ranks BELOW an
                      unrelated curated skill.
    q=code review  -> ['ruthless-mentor', 'clean-code', 'critical-code-reviewer']
                      `code-review` exists in the catalog and is not in the top 4.

Both are the same bug: relevance is not a sort term at the merge layer.

The fix threads the query into `rank()` and sorts on relevance tier FIRST, then
the existing (score, source, title) ladder as the within-tier tiebreak. With no
query every row is NEUTRAL_TIER, so browse ordering is byte-identical to before —
that property is pinned by `test_browse_ordering_unchanged_without_query`.
"""

from __future__ import annotations

import pytest

from app.services.federation_relevance import NEUTRAL_TIER, relevance_tier
from app.services.metasearch import UnifiedSkill, rank


def _skill(
    slug: str,
    *,
    title: str | None = None,
    description: str = "",
    source: str = "recipes",
    quality: str = "curated",
    popularity: int | None = None,
) -> UnifiedSkill:
    return UnifiedSkill(
        canonical_id=f"{source}:{slug}",
        slug=slug,
        title=title if title is not None else slug,
        description=description,
        source=source,
        origin_url=f"/skills/{slug}",
        install_ref=f"{source}:{slug}",
        quality=quality,
        deployable=True,
        install_path="fetch_origin",
        popularity=popularity,
        license=None,
        updated_at=None,
    )


class TestRelevanceOutranksBoost:
    """RED-proof: each of these fails on pre-fix `rank()` (no query term)."""

    def test_exact_slug_outranks_unrelated_curated_row(self) -> None:
        """q=seo — the live defect. `hundred-million-offers` is curated with real
        install signal; `seo` is a plain exact match. Relevance must win."""
        rows = [
            _skill(
                "hundred-million-offers",
                description="pricing strategy, offers, seo copy and positioning",
                popularity=5,
            ),
            _skill("seo", popularity=0),
        ]
        out = rank(rows, query="seo")
        assert out[0].slug == "seo", [s.slug for s in out]

    def test_exact_slug_outranks_curated_even_from_external_source(self) -> None:
        """Source priority must not resurrect the defect: an external exact match
        still beats a curated prose match."""
        rows = [
            _skill("hundred-million-offers", description="seo copywriting", popularity=99),
            _skill("seo", source="github-oss", quality="community", popularity=0),
        ]
        out = rank(rows, query="seo")
        assert out[0].slug == "seo", [s.slug for s in out]

    def test_multiword_query_slugifies_to_exact_match(self) -> None:
        """q='code review' — slugifies to `code-review`, which must rank first
        over curated prose matches."""
        rows = [
            _skill("ruthless-mentor", description="a ruthless code review mentor", popularity=9),
            _skill("clean-code", description="write clean code, review it", popularity=8),
            _skill("critical-code-reviewer", description="code review", popularity=7),
            _skill("code-review", popularity=0),
        ]
        out = rank(rows, query="code review")
        assert out[0].slug == "code-review", [s.slug for s in out]

    def test_prefix_beats_contains(self) -> None:
        rows = [
            _skill("agentic-seo-toolkit", description="seo", popularity=50),
            _skill("seo-audit", popularity=0),
        ]
        out = rank(rows, query="seo")
        assert out[0].slug == "seo-audit", [s.slug for s in out]

    def test_shortest_slug_wins_within_tier(self) -> None:
        """Phase B's polymark finding must survive the new outer sort term."""
        rows = [
            _skill("polymarket-worldcup-group-repricer", popularity=80),
            _skill("polymarket-manual-trade", popularity=70),
            _skill("polymarket", popularity=0),
        ]
        out = rank(rows, query="polymark")
        assert out[0].slug == "polymarket", [s.slug for s in out]


class TestNoRegression:
    """Phase B's wins and the browse path must be untouched."""

    def test_browse_ordering_unchanged_without_query(self) -> None:
        """No query -> every row is NEUTRAL_TIER -> the pre-fix ladder stands.

        This is the property that makes the change safe: browse is not a search.
        """
        rows = [
            _skill("alpha", quality="community", source="github-oss", popularity=1),
            _skill("beta", popularity=99),
            _skill("gamma", quality="community", source="skills-sh", popularity=50),
        ]
        assert [s.slug for s in rank(rows)] == [s.slug for s in rank(rows, query=None)]
        assert [s.slug for s in rank(rows, query="")] == [s.slug for s in rank(rows)]

    def test_curated_still_wins_at_equal_relevance(self) -> None:
        """_CURATED_BOOST is preserved as a WITHIN-tier tiebreak — the point of
        the fix is ordering of tiers, not deleting the boost.

        Slugs are the SAME LENGTH deliberately: the length term sorts before the
        score term, so an unequal-length pair would prove nothing about the
        boost. Titles are chosen so alphabetical order would put the COMMUNITY
        row first, meaning only the boost can produce the asserted order.
        """
        rows = [
            _skill(
                "seo-aaa",
                title="aaa",
                source="github-oss",
                quality="community",
                popularity=0,
            ),
            _skill("seo-zzz", title="zzz", source="recipes", quality="curated", popularity=0),
        ]
        out = rank(rows, query="seo")
        assert {s.slug for s in out} == {"seo-aaa", "seo-zzz"}
        assert out[0].slug == "seo-zzz", [s.slug for s in out]

    def test_rank_score_still_assigned(self) -> None:
        out = rank([_skill("a", popularity=5), _skill("b", popularity=1)], query="a")
        assert all(s.rank_score is not None for s in out)

    @pytest.mark.parametrize("q", ["seo", "code review", "polymark", "", None])
    def test_rank_is_total_and_preserves_membership(self, q: str | None) -> None:
        rows = [_skill("seo"), _skill("code-review"), _skill("polymarket")]
        out = rank(rows, query=q)
        assert sorted(s.slug for s in out) == sorted(s.slug for s in rows)


class TestTierContract:
    """Pin the assumption the fix rests on, so a tier-ladder edit reddens here."""

    def test_empty_query_is_neutral_tier(self) -> None:
        assert relevance_tier(None, slug="anything") == NEUTRAL_TIER
        assert relevance_tier("", slug="anything") == NEUTRAL_TIER

    def test_exact_slug_beats_description_match(self) -> None:
        exact = relevance_tier("seo", slug="seo", title="seo")
        prose = relevance_tier("seo", slug="hundred-million-offers", title="Offers", description="seo copy")
        assert exact < prose, (exact, prose)

    def test_multiword_query_slugifies(self) -> None:
        hit = relevance_tier("code review", slug="code-review", title="Code Review")
        miss = relevance_tier("code review", slug="ruthless-mentor", description="code review")
        assert hit < miss, (hit, miss)
