"""fed1006 — multi-word queries rank by token coverage, not by slug length.

Prod 2026-10-04, q="ASD-STE100 simplified technical english": the relevance
ladder matches the WHOLE phrase, so almost every row fell into NO_MATCH_TIER and
the next sort key — ``len(slug)`` — decided. ``n8n``, ``dots`` and ``dotfiles``
(GitHub code-search hits with no query word in slug, title or description)
ranked 3rd-6th; installable STE skills sat at 18 and below.

Contract: inside NO_MATCH_TIER only, rows sort by (weighted share of query
tokens found anywhere, weighted share found in slug/title) BEFORE anything
else, then score/source/title/canonical_id; slug length does not apply there. Rows that match a ladder tier
keep their exact previous order.
"""

from __future__ import annotations

from app.services.federation_relevance import NO_MATCH_TIER, relevance_tier
from app.services.metasearch import UnifiedSkill, rank

Q = "ASD-STE100 simplified technical english"


def _s(
    slug: str, description: str = "", *, title: str | None = None, source: str = "github-oss"
) -> UnifiedSkill:
    return UnifiedSkill(
        canonical_id=f"{source}:{slug}",
        slug=slug,
        title=title if title is not None else slug,
        description=description,
        source=source,
        origin_url=f"https://example.com/{slug}",
        install_ref=f"{source}:{slug}",
        quality="community",
        deployable=source != "github-oss",
        install_path="fetch_origin",
        popularity=None,
        license=None,
        updated_at=None,
    )


PROD_ROWS = [
    _s("n8n", "Fair-code workflow automation platform with native AI capabilities"),
    _s("dots", "Configuration I share across workspaces"),
    _s("dotfiles", "Personal dotfiles"),
    _s("avalanchego", "Go implementation of an Avalanche node."),
    _s(
        "simple-english",
        "Rewrite text to ASD-STE100 Simplified Technical English.",
        source="hermes-hub",
    ),
    _s(
        "simplified-technical-english-skill",
        "Reduce LLM word slop. Use ASD-STE100 Simplified Technical English rules.",
    ),
    _s("asd-ste100-skill", "Simplified Technical English for software documentation"),
]


def test_the_prod_case_puts_every_relevant_row_above_every_unrelated_one():
    no_match = {
        r.slug
        for r in PROD_ROWS
        if relevance_tier(Q, slug=r.slug, title=r.title, description=r.description) == NO_MATCH_TIER
    }
    # Two relevant rows contain the whole phrase (description tier); the third
    # relevant row and all four unrelated rows match no tier: the defect zone.
    assert no_match == {"n8n", "dots", "dotfiles", "avalanchego", "asd-ste100-skill"}
    order = [r.slug for r in rank(PROD_ROWS, query=Q)]
    relevant = {"simple-english", "simplified-technical-english-skill", "asd-ste100-skill"}
    assert set(order[:3]) == relevant, order
    assert set(order[3:]) == {"n8n", "dots", "dotfiles", "avalanchego"}


def test_more_tokens_covered_ranks_higher():
    rows = [
        _s("a", "english"),
        _s("b", "simplified technical english"),
        _s("c", "technical english"),
    ]
    assert [r.slug for r in rank(rows, query="simplified technical english guide")] == ["b", "c", "a"]


def test_a_title_hit_outweighs_the_same_hit_in_prose():
    rows = [
        _s("x-one", "a skill about english", title="Assistant"),
        _s("x-two", "a generic helper", title="English writer"),
    ]
    assert [r.slug for r in rank(rows, query="english prose style")][0] == "x-two"


def test_slug_length_no_longer_promotes_an_unrelated_row():
    rows = [_s("ab", "nothing here"), _s("long-slug-that-covers-docs", "technical docs writer")]
    assert rank(rows, query="technical docs style guide")[0].slug == "long-slug-that-covers-docs"


def test_rows_in_a_matching_tier_keep_their_previous_order():
    """A phrase match (any tier < NO_MATCH) still beats any token coverage, and
    inside a matching tier the shortest slug still wins (fdeloop0808 B2)."""
    rows = [
        _s("code-review-terry", "code review helper"),
        _s("code-review", "code review"),
        _s("reviewer", "code and review and code review tips everywhere"),
    ]
    order = [r.slug for r in rank(rows, query="code review")]
    assert order[:2] == ["code-review", "code-review-terry"]


def test_single_word_query_order_is_unchanged_for_matched_rows():
    rows = [_s("polymarket-markets"), _s("polymarket"), _s("polymarket-manual-trade")]
    assert [r.slug for r in rank(rows, query="polymark")] == [
        "polymarket",
        "polymarket-markets",
        "polymarket-manual-trade",
    ]


def test_no_query_path_is_a_fixed_sequence():
    rows = [_s("b", source="skills-sh"), _s("a"), _s("c", source="skills-sh")]
    # popularity percentile 0.5 for all; then source priority (skills-sh 10 <
    # github-oss 20), then title — exactly the pre-fed1006 browse comparator.
    assert [r.slug for r in rank(rows, query=None)] == ["b", "c", "a"]
    assert [r.slug for r in rank(rows, query="")] == ["b", "c", "a"]


# ── fed1006 R1 kill-tests ───────────────────────────────────────────────────


def test_r1_m1_no_match_order_is_independent_of_input_order():
    from app.services.metasearch import merge_unified

    rows = [_s("a", title="Helper"), _s("longer-name", title="Helper"), _s("zz", title="Helper")]
    first = [r.slug for r in rank(list(rows), query="pdf text")]
    for perm in (rows[::-1], [rows[1], rows[2], rows[0]]):
        assert [r.slug for r in rank(list(perm), query="pdf text")] == first
    merged = merge_unified([], rows, query="pdf text")
    merged_rev = merge_unified([], rows[::-1], query="pdf text")
    assert [r["slug"] if isinstance(r, dict) else r.slug for r in _skills_of(merged)] == [
        r["slug"] if isinstance(r, dict) else r.slug for r in _skills_of(merged_rev)
    ]


def _skills_of(merged):
    return getattr(merged, "skills", merged)


def test_r1_m2_short_tokens_match_whole_words_only():
    rows = [_s("email-django-tools", "send email from django"), _s("pdf-reader", "read pdf files")]
    assert rank(rows, query="ai go pdf")[0].slug == "pdf-reader"


def test_r1_m2_stopwords_and_generic_words_do_not_promote_junk():
    rows = [_s("a-to-text-tool", "a generic helper"), _s("pdf-to-text", "Extract documents")]
    assert rank(rows, query="a pdf to text tool")[0].slug == "pdf-to-text"


def test_r1_m2_stems_match_across_word_forms():
    rows = [_s("file-helper", "general files"), _s("image-converter", "convert one image")]
    assert rank(rows, query="converting images")[0].slug == "image-converter"


def test_r1_s1_the_tail_of_a_long_query_still_counts():
    rows = [
        _s("find-a-tool-to-convert-scanned", "generic"),
        _s("image-pdf-converter", "Scanned images into PDF"),
    ]
    assert rank(rows, query="find a tool to convert scanned images into pdf")[0].slug == "image-pdf-converter"


def test_r1_s2_a_matched_tier_ignores_coverage_even_when_it_differs():
    """Both rows are tier description_contains for q='code review'. Coverage
    would put 'longer-slug' (title hit) first; the matched tier must keep the
    shortest-slug order — this kills an 'apply coverage to every tier' mutant."""
    rows = [
        _s("longer-slug", "code review", title="Code tools"),
        _s("a", "code review", title="Helper"),
    ]
    tiers = {
        relevance_tier("code review", slug=r.slug, title=r.title, description=r.description) for r in rows
    }
    assert tiers == {5}
    assert [r.slug for r in rank(rows, query="code review")] == ["a", "longer-slug"]


# ── fed1006 R2 kill-tests ───────────────────────────────────────────────────


def _first(rows, q):
    return rank(rows, query=q)[0].slug


def test_r2_m1_domain_nouns_keep_their_intent():
    assert (
        _first([_s("memory-helper", "generic"), _s("plugin-index", "memory storage")], "plugin memory")
        == "plugin-index"
    )
    assert (
        _first([_s("memory-helper", "generic"), _s("agent-index", "memory storage")], "agent memory")
        == "agent-index"
    )
    assert (
        _first([_s("calling-helper", "generic"), _s("tool-index", "calling apis")], "tool calling")
        == "tool-index"
    )


def test_r2_m1_a_generic_word_cannot_outweigh_the_subject():
    assert (
        _first(
            [_s("a-to-text-tool", "a generic helper"), _s("pdf-to-text", "Extract documents")],
            "a pdf to text tool",
        )
        == "pdf-to-text"
    )


def test_r2_m2_cjk_tokens_match_inside_words():
    assert _first([_s("a", "generic"), _s("中文助手", "翻译服务")], "中文 翻译") == "中文助手"


def test_r2_m2_polish_words_stay_whole():
    from app.services.query_coverage import significant_tokens

    from app.services.query_coverage import fold

    assert significant_tokens("żółć gęślą") == [fold("żółć"), fold("gęślą")]
    assert _first([_s("g-l", "generic"), _s("żółć-helper", "gęślą wsparcie")], "żółć gęślą") == "żółć-helper"


def test_r2_m3_no_prefix_false_hits():
    assert (
        _first([_s("testament-harness", "generic"), _s("harness-kit", "test runner")], "test harness")
        == "harness-kit"
    )
    assert (
        _first([_s("reaction-hooks", "generic"), _s("hooks-kit", "React utilities")], "react hooks")
        == "hooks-kit"
    )
    assert (
        _first([_s("ste1000-pdf", "generic"), _s("ste100-reader", "PDF conversion")], "ste100 pdf")
        == "ste100-reader"
    )


def test_r2_m3_word_forms_still_match():
    from app.services.query_coverage import coverage

    for q, slug in [
        ("converting", "converter"),
        ("images", "image"),
        ("tests", "testing"),
        ("notes", "note-x"),
    ]:
        assert coverage([q], slug=slug, title="", description="")[0] == 1.0, (q, slug)


def test_r2_s1_a_late_discriminator_survives_the_token_cap():
    from app.services.query_coverage import coverage, significant_tokens

    q = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu target"
    tokens = significant_tokens(q)
    assert len(tokens) == 12 and "target" in tokens and "alpha" in tokens
    assert coverage(tokens, slug="target-index", title="", description="specific target functionality")[0] > 0


# ── fed1006 R3 kill-tests ───────────────────────────────────────────────────


def test_r3_m1_no_short_stem_conflation():
    assert (
        _first([_s("new-digest", "generic"), _s("news-reader", "digest headlines")], "news digest")
        == "news-reader"
    )
    assert (
        _first([_s("not-search", "generic"), _s("notes-index", "search notes")], "notes search")
        == "notes-index"
    )


def test_r3_m2_digit_tokens_are_exact_in_every_script():
    assert (
        _first([_s("模型20-翻译", "generic"), _s("模型2-index", "翻译服务")], "模型2 翻译") == "模型2-index"
    )
    assert (
        _first([_s("ニュース20-翻訳", "generic"), _s("ニュース2-index", "翻訳サービス")], "ニュース2 翻訳")
        == "ニュース2-index"
    )


def test_r3_s2_accents_and_dotted_i_fold():
    assert (
        _first([_s("maps-index", "İstanbul guide"), _s("istanbul-maps", "generic")], "İstanbul maps")
        == "istanbul-maps"
    )
    assert (
        _first([_s("recipes-index", "café list"), _s("cafe\u0301-recipes", "generic")], "café recipes")
        == "cafe\u0301-recipes"
    )


def test_r3_s3_a_final_short_subject_survives_a_long_query():
    from app.services.query_coverage import significant_tokens

    q = (
        "automated integration configuration deployment documentation validation "
        "generation conversion extraction processing analysis management pdf"
    )
    assert "pdf" in significant_tokens(q)
