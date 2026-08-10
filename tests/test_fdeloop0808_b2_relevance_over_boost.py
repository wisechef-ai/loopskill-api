"""fdeloop_0808 Phase B2 — query relevance must not be drowned by the
source/popularity boost in ``metasearch.rank()``.

## Context

PR #209 (Phase B) fixed recall: the per-source SQL truncation
(``federation_adapters.HermesHubAdapter.search``) now applies
``federation_relevance.relevance_order_clauses`` BEFORE ``LIMIT``, so an
exact-slug match survives the per-source cut. That fix operates entirely
WITHIN one source's candidate set.

It does **not** touch ``metasearch.rank()`` — the function that re-sorts the
MERGED candidate set (curated + every external source) into the final list
the user sees. That function's only signal is
``popularity_percentile_within_source + curated_boost``, with source-priority
and title as tiebreaks. It has no concept of "does this row's slug/title/
description actually match what the user typed" at all.

Two live-prod defects (2026-08-10) are exactly this: a candidate makes it
INTO the merged, already-relevance-filtered set (recall is fine — PR #209
fixed that layer), but the FINAL cross-source sort buries the strongest
lexical match under a curated-boost or popularity-percentile artifact that
has nothing to do with the query.

## Defect 1 — q=seo

Verified live against ``https://app.loopskill.io/api/skills/hundred-million-offers``:
its ``description`` field (the ONLY field ``unify_curated`` copies into
``UnifiedSkill.description`` — see ``metasearch_routes._curated_candidates``,
which copies ``d["description"]`` from ``_skill_to_out``, never ``readme``)
contains **no occurrence of "seo"** at all. The row only matched the curated
DB query because ``_curated_candidates`` also filters on ``Skill.readme``,
and "SEO agency exclusively for dental practices" / "Full competitive SEO
audit" appear in the README body, not the description. So the curated
candidate legitimately entered the merged set (correct recall), but has ZERO
lexical relevance to "seo" on any field ``rank()`` can see — and outranks the
skill literally slugged ``seo`` purely because ``_CURATED_BOOST`` (1.0) plus
its 5-install percentile beats an unrated hermes-hub row's 0.5 neutral prior.

## Defect 2 — q="code review"

Same shape: ``code-review`` (curated, 2 installs) is available and correct,
but three OTHER curated skills — ``ruthless-mentor`` (9 installs, mentions
"Code review" only in its README, not its description), ``clean-code``
(5 installs, description literally contains 'code review'), and
``critical-code-reviewer`` (5 installs, description contains "code reviews")
— outrank it purely on install-count popularity + curated boost, even
though ``code-review``'s SLUG is an exact match and the others' slugs are not.

The fix cannot re-derive within-source SQL ordering (out of scope, PR #209's
job) — it has to give ``rank()`` a lexical-relevance PRIMARY sort key,
computed from the SAME ``relevance_tier`` ladder PR #209 already built for
the SQL layer (``app/services/federation_relevance.py``), applied to the
already-unified ``UnifiedSkill`` (slug, title, description). Popularity/
curated-boost then only breaks ties WITHIN a tier — never overrides one.
"""

from __future__ import annotations

from app.services.federation import ExternalSkill, InstallPath
from app.services.metasearch import merge_unified, rank, unify_curated, unify_external


def _ext(source: str, slug: str, *, title: str = "", description: str = "") -> ExternalSkill:
    return ExternalSkill(
        slug=slug,
        title=title or slug,
        source=source,
        install_path=InstallPath.FETCH_ORIGIN,
        origin_url=f"https://{source}/{slug}",
        license=None,
        redistributable=True,
        description=description,
    )


def _curated_row(slug: str, *, title: str = "", description: str = "", install_count: int = 0) -> dict:
    return {
        "slug": slug,
        "title": title or slug,
        "description": description,
        "install_count": install_count,
    }


# ── Defect 1: q=seo ──────────────────────────────────────────────────────


def _seo_scene():
    """Mirrors the live prod shapes verified 2026-08-10.

    ``hundred-million-offers``: curated, 5 installs, description carries NO
    occurrence of "seo" (verified against the live API — the README does,
    the description does not). ``seo``: hermes-hub, unrated (no popularity
    signal at all — a fresh federated snapshot row).
    """
    curated = [
        unify_curated(
            _curated_row(
                "hundred-million-offers",
                title="hundred-million-offers",
                description=(
                    "Create irresistible offers using the Value Equation, bonus "
                    "stacking, risk-reversing guarantees, and ethical scarcity."
                ),
                install_count=5,
            )
        )
    ]
    external = [
        unify_external(
            _ext(
                "hermes-hub",
                "seo",
                title="SEO (Site Audit + Content Writer + Competitor Analysis)",
                description="Full SEO toolkit: site audit, content writer, competitor analysis.",
            ),
            raw_row={},
        ),
        unify_external(
            _ext(
                "hermes-hub",
                "seo-audit",
                title="SEO Audit",
                description="Run a full technical SEO audit.",
            ),
            raw_row={},
        ),
    ]
    return curated, external


class TestDefect1ExactSlugVsPopularityBoost:
    def test_RED_current_rank_buries_exact_slug_under_unrelated_curated_boost(self):
        """RED: proves the defect using the REAL rank()/merge_unified() pipeline,
        exactly as ``metasearch_routes._build`` calls it today (no query
        threaded through at all). ``hundred-million-offers`` has NO lexical
        relevance to "seo" on any field rank() can see, yet the curated boost
        (+1.0) plus its 5-install percentile currently outranks the exact
        slug match. This must currently rank hundred-million-offers FIRST —
        i.e. it demonstrates the bug, not the fix.
        """
        curated, external = _seo_scene()
        result = merge_unified(curated, external)
        slugs = [s.slug for s in result.skills]
        assert slugs[0] == "hundred-million-offers", (
            "if this fails, the defect has already been fixed upstream of this "
            f"test — got {slugs}"
        )

    def test_exact_slug_must_outrank_unrelated_curated_boost_when_query_aware(self):
        """The target behaviour: once ``rank``/``merge_unified`` are made query-
        aware, the exact slug match for the query term must win regardless of
        curated status or popularity, because curated relevance here is zero.
        """
        curated, external = _seo_scene()
        result = merge_unified(curated, external, query="seo")
        slugs = [s.slug for s in result.skills]
        assert slugs[0] == "seo", f"exact slug match must rank first for q=seo: {slugs}"


# ── Defect 2: q="code review" ────────────────────────────────────────────


def _code_review_scene():
    """Mirrors the live prod shapes verified 2026-08-10: three curated skills
    outrank the exact-slug ``code-review`` purely on install-count + curated
    boost, though only ``code-review`` has slug relevance to the query."""
    curated = [
        unify_curated(
            _curated_row(
                "ruthless-mentor",
                title="ruthless-mentor",
                description=(
                    "Stress-test the team's plan, idea, or decision in attack mode "
                    "— sort proposals into gold / trash / directionally-right-but-flawed."
                ),
                install_count=9,
            )
        ),
        unify_curated(
            _curated_row(
                "clean-code",
                title="clean-code",
                description=(
                    "Write readable, maintainable code through disciplined naming, "
                    "small functions, and clean error handling. Use when the user "
                    "mentions 'code review', 'naming conventions'."
                ),
                install_count=5,
            )
        ),
        unify_curated(
            _curated_row(
                "critical-code-reviewer",
                title="critical-code-reviewer",
                description=(
                    "Conduct rigorous, adversarial code reviews with zero tolerance "
                    "for mediocrity."
                ),
                install_count=5,
            )
        ),
        unify_curated(
            _curated_row(
                "code-review",
                title="code-review",
                description="Guidelines for performing thorough code reviews with security and quality focus",
                install_count=2,
            )
        ),
    ]
    external = [
        unify_external(
            _ext("hermes-hub", "code-review", title="Code Review", description="Code review skill."),
            raw_row={},
        ),
        unify_external(
            _ext(
                "hermes-hub",
                "code-review-terry",
                title="Code Review Terry",
                description="Terry's code review skill.",
            ),
            raw_row={},
        ),
    ]
    return curated, external


class TestDefect2MultiwordQueryMissesExactSlug:
    def test_RED_current_rank_ranks_popular_curated_skills_above_exact_slug(self):
        """RED: reproduces prod's ``q="code review"`` top-4
        (ruthless-mentor, clean-code, critical-code-reviewer, ...) with
        ``code-review`` NOT first, using the real pipeline as called today
        (no query threaded through merge_unified/rank at all)."""
        curated, external = _code_review_scene()
        result = merge_unified(curated, external)
        slugs = [s.slug for s in result.skills]
        assert slugs[0] != "code-review", (
            "if this fails, the defect has already been fixed upstream of this "
            f"test — got {slugs}"
        )
        assert slugs[0] == "ruthless-mentor", f"expected the prod-observed top1: {slugs}"

    def test_exact_slug_wins_multiword_query_when_query_aware(self):
        curated, external = _code_review_scene()
        result = merge_unified(curated, external, query="code review")
        slugs = [s.slug for s in result.skills]
        assert slugs[0] == "code-review", f"exact slug (via slugified query) must rank first: {slugs}"

    def test_curated_still_wins_ties_within_the_same_relevance_tier(self):
        """Both the curated and hermes-hub 'code-review' rows are exact-slug
        matches (same relevance tier). The pre-existing curated-wins-ties
        contract (plan §5.4 / _CURATED_BOOST) must still decide between them —
        the query-aware primary key does not erase the existing tiebreak."""
        curated, external = _code_review_scene()
        result = merge_unified(curated, external, query="code review")
        top = result.skills[0]
        assert top.slug == "code-review"
        assert top.quality == "curated", (
            f"curated must still win the within-tier tie over the hermes-hub "
            f"duplicate: {top.source}"
        )


class TestRankIsBackwardCompatibleWithoutQuery:
    def test_rank_without_query_argument_still_works(self):
        """rank() must remain callable with the pre-existing signature (no
        query) for any caller that doesn't have one — must not raise, and must
        preserve the original popularity+curated-boost ordering contract."""
        curated, external = _seo_scene()
        merged = curated + external
        out = rank(merged)
        assert len(out) == len(merged)

    def test_merge_unified_without_query_still_works(self):
        curated, external = _seo_scene()
        result = merge_unified(curated, external)
        assert len(result.skills) == len(curated) + len(external)


# ── Defect 3 (regression guard): tier tiebreak must not undo PR #209's
#    shortest-slug-wins tiebreak at the CROSS-SOURCE rank layer ─────────────


class TestCrossSourceRankDoesNotUndoShortestSlugTiebreak:
    """PR #209 fixed ``q=polymark`` at the per-SOURCE SQL truncation layer by
    adding a shortest-slug-wins tiebreak. If the new query-aware ``rank()``
    only adds a relevance-tier primary key and falls through to the OLD
    (source_priority, title-alphabetical) tiebreak, three same-source,
    same-popularity hermes-hub rows sharing the ``slug_prefix`` tier would be
    re-sorted ALPHABETICALLY by title at the cross-source layer — silently
    re-introducing the exact defect PR #209 fixed, one layer up.
    """

    def test_RED_shortest_slug_tiebreak_must_survive_into_cross_source_rank(self):
        external = [
            unify_external(
                _ext(
                    "hermes-hub",
                    "polymarket-worldcup-group-repricer",
                    title="Aaa Polymarket Repricer",  # sorts first alphabetically
                ),
                raw_row={},
            ),
            unify_external(
                _ext(
                    "hermes-hub",
                    "polymarket-manual-trade",
                    title="Bbb Polymarket Manual Trade",
                ),
                raw_row={},
            ),
            unify_external(
                _ext("hermes-hub", "polymarket", title="Zzz Polymarket Core"),
                raw_row={},
            ),
        ]
        result = merge_unified([], external, query="polymark")
        slugs = [s.slug for s in result.skills]
        assert slugs[0] == "polymarket", (
            f"shortest-slug tiebreak from PR #209 must survive cross-source "
            f"rank, even when alphabetically last: {slugs}"
        )


# ── The judged set: >=15 (query, expected) pairs against the FULL merged
#    pipeline (curated + external -> merge_unified -> final list) ──────────
#
# This is deliberately at a DIFFERENT layer than PR #209's judged set
# (scripts/fdeloop0808_judged_search.py), which scores the per-source SQL
# truncation. This one scores the CROSS-SOURCE final ranking a real caller of
# metasearch_routes.metasearch() sees — the layer both live defects live on.
# It reuses shapes verified against the live API (2026-08-10) plus PR #209's
# own judged queries so a regression on either layer is caught by ONE gate.


def _judged_corpus():
    """One corpus, multiple queries scored against it — mirrors the shape of
    scripts/fdeloop0808_judged_search.py's JUDGED list, one layer up."""
    curated = [
        unify_curated(_curated_row("hundred-million-offers", description="pricing and offers", install_count=5)),
        unify_curated(
            _curated_row(
                "ruthless-mentor",
                description="Stress-test the team's plan in attack mode.",
                install_count=9,
            )
        ),
        unify_curated(
            _curated_row(
                "clean-code",
                description="Write readable code. Use when the user mentions 'code review'.",
                install_count=5,
            )
        ),
        unify_curated(
            _curated_row(
                "critical-code-reviewer",
                description="Conduct rigorous, adversarial code reviews.",
                install_count=5,
            )
        ),
        unify_curated(_curated_row("code-review", description="Guidelines for code reviews.", install_count=2)),
        unify_curated(_curated_row("obviously-awesome", description="Positioning framework.", install_count=20)),
    ]
    external = [
        unify_external(
            _ext("hermes-hub", "seo", title="SEO Toolkit", description="Site audit + content writer."),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "seo-audit", title="SEO Audit", description="Technical SEO audit."),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "ai-seo", title="Ai Seo", description="AI-driven SEO."),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "code-review", title="Code Review", description="Code review skill."),
            raw_row={},
        ),
        unify_external(
            _ext(
                "hermes-hub", "code-review-terry", title="Code Review Terry", description="Terry's reviews."
            ),
            raw_row={},
        ),
        unify_external(
            _ext(
                "hermes-hub",
                "polymarket-worldcup-group-repricer",
                title="Aaa Polymarket Repricer",
            ),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "polymarket-manual-trade", title="Bbb Polymarket Manual Trade"),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "polymarket", title="Zzz Polymarket Core"),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "excalidraw", title="Excalidraw", description="Hand-drawn diagrams."),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "excalidraw-templates", title="Excalidraw Templates"),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "whisper", title="Whisper", description="Speech recognition."),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "arxiv", title="Arxiv", description="Paper search."),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "humanizer", title="Humanizer", description="Remove AI writing tells."),
            raw_row={},
        ),
        unify_external(
            _ext(
                "hermes-hub",
                "test-driven-development",
                title="Test Driven Development",
                description="Red-green-refactor.",
            ),
            raw_row={},
        ),
        unify_external(
            _ext("hermes-hub", "nano-banana-pro", title="Nano Banana Pro", description="Image generation."),
            raw_row={},
        ),
        unify_external(
            _ext(
                "hermes-hub",
                "knowledge-graph",
                title="Knowledge Graph",
                description="Build interconnected knowledge graphs.",
            ),
            raw_row={},
        ),
        unify_external(
            _ext(
                "hermes-hub",
                "copy-doctor",
                title="Copy Doctor",
                description="Improve marketing copywriting.",
            ),
            raw_row={},
        ),
    ]
    return curated, external


# (query, expected_top1)
JUDGED: list[tuple[str, str]] = [
    ("seo", "seo"),
    ("code review", "code-review"),
    ("code-review", "code-review"),
    ("polymark", "polymarket"),
    ("polymarket", "polymarket"),
    ("excalid", "excalidraw"),
    ("excalidraw", "excalidraw"),
    ("whisper", "whisper"),
    ("arxiv", "arxiv"),
    ("humanizer", "humanizer"),
    ("test-driven-development", "test-driven-development"),
    ("nano-banana-pro", "nano-banana-pro"),
    ("knowledge graph", "knowledge-graph"),
    ("copywriting", "copy-doctor"),
    ("ai-seo", "ai-seo"),
    ("seo-audit", "seo-audit"),
]


class TestJudgedSetCrossSourceTop1:
    """Runs the ENTIRE judged set through the real ``merge_unified`` pipeline.
    Must be 100% at HEAD (post-fix) and MUST currently fail (RED) on the two
    prod defect queries before the fix lands.
    """

    def test_RED_current_code_fails_the_two_known_defects(self):
        curated, external = _judged_corpus()
        failures = []
        for q, expected in JUDGED:
            result = merge_unified(curated, external)  # no query threaded — today's behaviour
            slugs = [s.slug for s in result.skills]
            top1 = slugs[0] if slugs else None
            if top1 != expected:
                failures.append((q, expected, top1))
        failing_queries = {q for q, _, _ in failures}
        assert "seo" in failing_queries, f"expected q=seo to fail without query-aware ranking: {failures}"
        assert "code review" in failing_queries, (
            f"expected q='code review' to fail without query-aware ranking: {failures}"
        )

    def test_judged_set_100pct_top1_with_query_aware_ranking(self):
        curated, external = _judged_corpus()
        failures = []
        for q, expected in JUDGED:
            result = merge_unified(curated, external, query=q)
            slugs = [s.slug for s in result.skills]
            top1 = slugs[0] if slugs else None
            if top1 != expected:
                failures.append((q, expected, top1, slugs[:5]))
        assert not failures, f"judged-set top1 failures: {failures}"
