"""fdeloop_0808 Phase B — search recall, fixed at the layer that loses it.

## The defect, measured on prod 2026-08-08

``federation_adapters.HubAdapter.search`` builds a correct ``ILIKE`` predicate
across title/description/identifier/slug, and then does this::

    rows = db_q.order_by(FederationHubSkill.title).limit(limit).all()

``q=seo`` matches **676** rows in ``federation_hub_skills``. The adapter keeps
25 of them — the 25 that sort first ALPHABETICALLY. Measured live::

    aaddyy-ai-tools, add-json-ld, affiliate-marketing-auto,
    affiliate-marketing-auto-lvjunjie, affiliate-marketing-generator,
    agent-seo-engine, ahrefs-seo, ahrefs, ...

The skill whose slug is literally ``seo`` is **not in that set** and can never
be, at any page size below the number of matches sorting before "s". Neither
are ``ai-seo``, ``seopro``, ``seo-geo``, or ``seo-agi``.

This is why the council called it *"a recall failure masquerading as a sorting
problem"*: the downstream ranker in ``metasearch.rank()`` is handed 25 rows
that never contained the right answer. Improving that ranker re-sorts a set the
correct row was already excluded from. **Relevance has to be applied BEFORE the
truncation, in SQL.**

## What is asserted here

A JUDGED QUERY SET, not one assertion. The predecessor's single
``assert "seo" in results`` was, in the council's words, "laughably weak" — it
passes against alphabetical output for any query whose match happens to sort
early.

Cases cover: exact-slug recall, prefix, whole-word title, description-only
matches, and the specific ordering invariants a ranked-before-truncated query
must hold. Every fixture row is modelled on a real row shape from the live
90,605-row hub snapshot.
"""

from __future__ import annotations

import pytest

from app.models import FederationHubSkill


# ── corpus ───────────────────────────────────────────────────────────────


def _hub_row(db, slug: str, title: str, description: str = "", identifier: str | None = None):
    row = FederationHubSkill(
        source="hermes-hub",
        slug=slug,
        identifier=identifier or f"hermes-hub/{slug}",
        title=title,
        description=description,
        origin_url=f"https://example.invalid/skills/{slug}",
        install_path="fetch_origin",
    )
    db.add(row)
    return row


@pytest.fixture
def hub_corpus(db_session):
    """A miniature of the live shape: many alphabetically-early near-misses that
    bury the exact match, plus the exact match itself.

    The ``aaa-*`` block reproduces the real failure — on prod, six
    ``a*``-titled rows preceded ``seo`` in the alphabetical cut. If the adapter
    truncates before scoring, these are the rows that survive and ``seo`` is
    the row that dies.
    """
    for i in range(30):
        _hub_row(
            db_session,
            f"aaa-noise-{i:02d}",
            f"AAA Noise {i:02d}",
            description="A skill that merely mentions seo somewhere in its description.",
        )
    _hub_row(db_session, "seo", "SEO (Site Audit + Content Writer + Competitor Analysis)")
    _hub_row(db_session, "seo-geo", "SEO GEO for SaaS")
    _hub_row(db_session, "ai-seo", "Ai Seo")
    _hub_row(db_session, "agent-seo-engine", "Agent SEO Engine")
    _hub_row(db_session, "zzz-last", "Zzz Last", description="unrelated to the query term")
    _hub_row(
        db_session,
        "humanizer",
        "Humanizer",
        description="Remove signs of AI-generated writing from text.",
    )
    _hub_row(
        db_session,
        "copy-doctor",
        "Copy Doctor",
        description="Improve marketing copywriting and landing page conversion.",
    )
    db_session.commit()
    return db_session


@pytest.fixture
def adapter(hub_corpus, monkeypatch):
    """A HubAdapter wired at the test session (it opens its own SessionLocal)."""
    from app.services import federation_adapters as fa

    class _NoCloseSession:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):  # the adapter closes its session; the fixture owns it
            pass

    monkeypatch.setattr(
        "app.database.SessionLocal", lambda: _NoCloseSession(hub_corpus), raising=False
    )
    return fa.HermesHubAdapter(fetch=lambda _q: [])


def _slugs(results):
    return [r.slug for r in results]


# ── the recall gate ──────────────────────────────────────────────────────


class TestRecallBeforeTruncation:
    def test_exact_slug_match_survives_a_small_limit(self, adapter):
        """THE Phase-B gate. 30 alphabetically-earlier rows also match ``seo``
        in their description. Under ``order_by(title).limit(5)`` the exact-slug
        row is unreachable — this is the live prod defect in miniature.
        """
        results = adapter.search("seo", limit=5)
        assert "seo" in _slugs(results), (
            "exact-slug match lost to the alphabetical cut — recall failure, "
            f"got {_slugs(results)}"
        )

    def test_exact_slug_match_ranks_first(self, adapter, hub_corpus):
        """An exact slug match comes first — via the length tiebreak, not a
        dedicated tier.

        The first draft had an ``exact_slug`` tier above ``slug_prefix``. The
        RED-proof could not make ANY test redden when it was deleted, and that
        turned out to be a true finding: an exact match is always also a prefix
        match, and always the shortest one, so ``func.length(slug)`` already
        orders it first. The redundant branch was deleted; this test pins the
        surviving behaviour.
        """
        slugs = _slugs(adapter.search("seo", limit=10))
        assert slugs[0] == "seo", f"exact slug must rank first: {slugs}"

    def test_exact_match_beats_a_longer_sibling_sorting_earlier(self, adapter, hub_corpus):
        """The alphabetical tiebreak must not be able to overturn it."""
        _hub_row(hub_corpus, "seo-aaa-first-alphabetically", "Aaa Seo Variant")
        hub_corpus.commit()

        assert _slugs(adapter.search("seo", limit=10))[0] == "seo"

    def test_prefix_match_outranks_a_SHORTER_contains_match(self, adapter, hub_corpus):
        """The prefix tier must beat the contains tier even when the contains
        row has the shorter slug — otherwise the length tiebreak is doing all
        the work and the tier is decoration.

        ``x-seo`` (5 chars, contains) vs ``seo-tool`` (8 chars, prefix): only
        the tier ordering can put ``seo-tool`` first. The RED-proof rejected
        two earlier fixtures where length alone produced the right answer.
        """
        _hub_row(hub_corpus, "x-seo", "X Seo")
        _hub_row(hub_corpus, "seo-tool", "Seo Tool")
        hub_corpus.commit()

        slugs = [s for s in _slugs(adapter.search("seo", limit=20)) if s in {"x-seo", "seo-tool"}]
        assert slugs[0] == "seo-tool", (
            f"prefix tier must outrank a shorter contains match: {slugs}"
        )

    def test_slug_matches_outrank_description_only_matches(self, adapter):
        """A row whose SLUG contains the term is a stronger answer than one that
        merely mentions it in prose."""
        results = adapter.search("seo", limit=10)
        slugs = _slugs(results)
        first_noise = next((i for i, s in enumerate(slugs) if s.startswith("aaa-noise")), len(slugs))
        for strong in ("seo", "seo-geo", "ai-seo"):
            assert slugs.index(strong) < first_noise, (
                f"{strong} (slug match) ranked below description-only noise: {slugs}"
            )

    def test_title_match_outranks_description_match(self, adapter):
        results = adapter.search("humanizer", limit=10)
        assert _slugs(results)[0] == "humanizer"

    def test_description_only_match_is_still_returned(self, adapter):
        """Recall is not sacrificed for precision: a description-only hit must
        still be findable when nothing stronger exists."""
        results = adapter.search("copywriting", limit=10)
        assert "copy-doctor" in _slugs(results)

    def test_non_matching_rows_are_never_returned(self, adapter):
        results = adapter.search("seo", limit=50)
        assert "zzz-last" not in _slugs(results)

    def test_limit_is_respected(self, adapter):
        assert len(adapter.search("seo", limit=3)) == 3

    def test_empty_query_returns_rows_without_error(self, adapter):
        """An empty query is a browse; the relevance ordering must degrade to a
        stable listing rather than raising."""
        assert len(adapter.search("", limit=5)) == 5

    def test_case_insensitive_recall(self, adapter):
        assert "seo" in _slugs(adapter.search("SEO", limit=5))

    def test_no_match_returns_empty(self, adapter):
        assert adapter.search("zzzz-no-such-term-anywhere", limit=10) == []


# ── the scoring primitive, unit-tested away from the DB ──────────────────


class TestRelevanceOrdering:
    """``relevance_order_clauses`` is the SQL expression that replaces
    ``order_by(title)``. Testing the tiers directly keeps the contract legible
    when someone later adds a signal."""

    def test_tier_ranks_slug_prefix_highest(self):
        """An exact match is a prefix match; the length tiebreak (not a
        dedicated tier) is what separates it from longer siblings."""
        from app.services.federation_relevance import relevance_tier

        assert relevance_tier("seo", slug="seo", title="SEO", description="") == 0

    def test_tier_ranks_slug_prefix_above_slug_contains(self):
        from app.services.federation_relevance import relevance_tier

        prefix = relevance_tier("seo", slug="seo-geo", title="SEO GEO", description="")
        contains = relevance_tier("seo", slug="ai-seo", title="Ai Seo", description="")
        assert prefix < contains

    def test_tier_ranks_slug_above_title_only(self):
        from app.services.federation_relevance import relevance_tier

        slug_hit = relevance_tier("seo", slug="ai-seo", title="Unrelated", description="")
        title_hit = relevance_tier("seo", slug="unrelated", title="SEO Toolkit", description="")
        assert slug_hit < title_hit

    def test_tier_ranks_title_above_description_only(self):
        from app.services.federation_relevance import relevance_tier

        title_hit = relevance_tier("seo", slug="x-y", title="SEO Toolkit", description="")
        desc_hit = relevance_tier("seo", slug="x-y", title="Unrelated", description="does seo")
        assert title_hit < desc_hit

    def test_non_match_sorts_last(self):
        from app.services.federation_relevance import relevance_tier

        worst = relevance_tier("seo", slug="a", title="b", description="c")
        any_hit = relevance_tier("seo", slug="a", title="b", description="seo")
        assert worst > any_hit

    def test_empty_query_is_uniformly_neutral(self):
        """With no query every row is equally relevant; ordering must fall
        through to the stable tiebreak instead of inventing a ranking.

        Asserting only that two rows TIE is not enough — an empty string is a
        prefix of everything, so a broken implementation ties them too (at
        ``slug_prefix``, tier 1). The RED-proof caught that. The property that
        actually distinguishes the two is the tier VALUE: ``NEUTRAL_TIER``,
        reached by the explicit empty-query guard.
        """
        from app.services.federation_relevance import NEUTRAL_TIER, relevance_tier

        a = relevance_tier("", slug="alpha", title="Alpha", description="")
        b = relevance_tier("", slug="zulu", title="Zulu", description="")
        assert a == b == NEUTRAL_TIER

    def test_empty_query_emits_no_ordering_clause(self):
        """The SQL side of the same property: a browse must not carry a
        relevance term at all, or every row's ``ilike('%')`` match would make
        the CASE expression pure overhead on the biggest scan we run."""
        from app.models import FederationHubSkill
        from app.services.federation_relevance import relevance_order_clauses

        assert relevance_order_clauses(FederationHubSkill, "") == []
        assert relevance_order_clauses(FederationHubSkill, None) == []
        assert relevance_order_clauses(FederationHubSkill, "   ") == []


# ── multi-word queries: slug shape vs prose shape ────────────────────────


class TestQuerySlugification:
    """A user types ``code review``; the identifier is ``code-review``.

    Measured on prod 2026-08-08 before this was added: ``q="code review"``
    returned ``eb-code-review``, ``codereview-assistant``, ``code-review-terry``
    ABOVE ``code-review`` itself — the exact match was demoted to a title tier
    because the raw query string never appears in a hyphenated slug.
    """

    def test_multiword_query_becomes_slug_shape(self):
        from app.services.federation_relevance import slugify_query

        assert slugify_query("code review") == "code-review"
        assert slugify_query("  Knowledge   Graph  ") == "knowledge-graph"

    def test_single_word_query_is_unchanged(self):
        from app.services.federation_relevance import slugify_query

        assert slugify_query("seo") == "seo"

    def test_multiword_query_hits_the_top_slug_tier(self):
        from app.services.federation_relevance import relevance_tier

        assert relevance_tier("code review", slug="code-review", title="Code Review") == 0

    def test_prose_tiers_keep_the_raw_query(self):
        """Slugifying the description predicate would stop ``code review`` from
        matching the words "code review" in prose — the fix must not cost
        recall on the tier that needs it most."""
        from app.services.federation_relevance import relevance_tier, NO_MATCH_TIER

        tier = relevance_tier(
            "code review", slug="x-y", title="Unrelated", description="does a code review for you"
        )
        assert tier < NO_MATCH_TIER

    def test_multiword_query_recall_end_to_end(self, adapter, hub_corpus):
        _hub_row(hub_corpus, "code-review", "Code Review")
        _hub_row(hub_corpus, "eb-code-review", "Eb Code Review")
        _hub_row(hub_corpus, "code-review-terry", "Code Review Terry")
        hub_corpus.commit()

        assert _slugs(adapter.search("code review", limit=5))[0] == "code-review"


# ── within-tier tiebreak ─────────────────────────────────────────────────


class TestShortestSlugTiebreak:
    """Within one relevance tier, the shortest slug wins.

    Measured on prod: ``q=polymark`` put ``polymarket-worldcup-group-repricer``,
    ``polymarket-manual-trade`` and ``polymarket-markets`` above plain
    ``polymarket``. All four are ``slug_prefix`` matches, so the alphabetical
    tiebreak decided — and alphabetical order has no relationship to which row
    the user meant. A prefix query most likely wants the row closest to being
    the term itself.
    """

    def test_shortest_slug_wins_within_a_tier(self, adapter, hub_corpus):
        """Titles are chosen so the ALPHABETICAL tiebreak actively disagrees
        with the length tiebreak. If they agreed, this test would pass with the
        length term removed and would prove nothing — the RED-proof caught
        exactly that in the first draft."""
        _hub_row(hub_corpus, "polymarket", "Zzz Polymarket Core")
        _hub_row(hub_corpus, "polymarket-manual-trade", "Aaa Polymarket Manual Trade")
        _hub_row(hub_corpus, "polymarket-worldcup-group-repricer", "Bbb Polymarket Repricer")
        hub_corpus.commit()

        slugs = _slugs(adapter.search("polymark", limit=5))
        assert slugs[0] == "polymarket", (
            f"shortest slug must win within a tier even when alphabetically last: {slugs}"
        )

    def test_tiebreak_never_overrides_tier(self, adapter, hub_corpus):
        """A short slug in a WEAK tier must not beat a long slug in a strong
        one — the tiebreak is secondary by construction."""
        _hub_row(hub_corpus, "a-b", "Unrelated", description="mentions kubernetes in prose")
        _hub_row(hub_corpus, "kubernetes-cluster-autoscaler", "K8s Autoscaler")
        hub_corpus.commit()

        assert _slugs(adapter.search("kubernetes", limit=5))[0] == "kubernetes-cluster-autoscaler"
