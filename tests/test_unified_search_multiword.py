"""RED-proof for the multi-word unified-search blackout (atomic-habits 2026-09-13 rank 1).

THE BUG. Every group in ``app/services/unified_search.py`` built its predicate as
``f"%{q}%"`` — a single ILIKE over the WHOLE query string. A two-word query
therefore only matched a row containing that exact adjacent phrase, so
GET /api/search?q=<two words> returned zero rows in every group unless the
literal phrase happened to be present.

Measured on production app.loopskill.io at 2026-09-13T21:05 CEST:

    q=csv          -> skills 0, federated 20
    q=cleanup      -> skills 0, federated 20
    q=csv+cleanup  -> skills 0, federated  0     <-- both terms have hits, AND has none
    q=pdf          -> skills 3, federated 20
    q=pdf+extract  -> skills 0, federated 18     <-- 3 skills vanish

That is the entire multi-word query space of the search box returning nothing.

THE CONTRACT these tests pin:
  * terms are AND-ed (a row must match every term) and columns are OR-ed
    (a term may match in any of that group's searchable columns);
  * the semantic holds in EVERY group — skills, loops, bundles, personalities,
    connectors, and both legs of federated (hub table + first_page cache);
  * SINGLE-term queries keep byte-identical behaviour (no regression);
  * empty/whitespace queries keep matching everything, as before;
  * visibility filters are untouched — a widened predicate must not leak a
    private/archived row;
  * term count is bounded so a pathological query cannot build an unbounded
    predicate;
  * the federated hub predicate keeps matching the ONE concatenated blob
    expression the issue-#282 GIN trigram index is built on. Three OR'd column
    ILIKEs would de-optimise the planner back to a seq scan (812ms vs
    0.1-15ms, measured in tests/migrations/test_issue282_fed_hub_trgm.py).

These exercise the REAL query functions against a REAL database session, so a
passing run is proof the generated SQL matches — not proof that a mock agreed
with itself.
"""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest

from app.models import Bundle, Connector, FederationHubSkill, Personality, Skill, Verifier
from app.services import unified_search as us


# ── seeding ──────────────────────────────────────────────────────────────────


def _skill(db, slug, title, description, category="devops", is_public=True, is_archived=False):
    row = Skill(
        id=uuid4(),
        slug=slug,
        title=title,
        description=description,
        category=category,
        is_public=is_public,
        is_archived=is_archived,
    )
    db.add(row)
    db.flush()
    return row


def _verifier(db, slug, title, description):
    row = Verifier(
        id=uuid4(),
        slug=slug,
        title=title,
        description=description,
        success_condition="ok",
        verification_script="pytest -q",
        system_prompt="drive to green",
        max_turns=25,
        stopping_criteria={"success": "a", "failure": "b", "budget": "c"},
        tool_allowlist=["terminal"],
        is_public=True,
        is_archived=False,
        run_count=1,
    )
    db.add(row)
    db.flush()
    return row


def _bundle(db, slug, name, description):
    row = Bundle(id=uuid4(), slug=slug, name=name, description=description, visibility="public")
    db.add(row)
    db.flush()
    return row


def _personality(db, slug, title, description):
    row = Personality(
        id=uuid4(),
        slug=slug,
        title=title,
        description=description,
        system_prompt="be useful",
        is_public=True,
        is_archived=False,
    )
    db.add(row)
    db.flush()
    return row


def _connector(db, slug, title):
    row = Connector(
        id=uuid4(),
        slug=slug,
        title=title,
        connector_type="mcp",
        is_public=True,
        is_archived=False,
    )
    db.add(row)
    db.flush()
    return row


def _hub_skill(db, slug, title, description, source="hermes-hub"):
    row = FederationHubSkill(
        slug=slug,
        title=title,
        description=description,
        source=source,
        origin_url=f"https://example.test/{slug}",
    )
    db.add(row)
    db.flush()
    return row


def _slugs(rows):
    return {r["slug"] for r in rows}


# ── the reported bug, per group ──────────────────────────────────────────────


def test_skills_two_word_query_matches_row_carrying_both_words_non_adjacently(db_session):
    """The exact production failure: 'csv cleanup' must find a CSV-cleanup skill."""
    _skill(db_session, "csv-toolkit", "CSV Toolkit", "Cleanup and normalisation for messy spreadsheet exports.")
    assert _slugs(us.search_skills_group(db_session, "csv cleanup", 10)) == {"csv-toolkit"}


def test_skills_two_word_query_still_misses_a_row_carrying_only_one_word(db_session):
    """AND, not OR — 'csv cleanup' must not drag in every CSV row."""
    _skill(db_session, "csv-json", "CSV Toolkit", "Convert spreadsheets to JSON.")
    assert us.search_skills_group(db_session, "csv cleanup", 10) == []


def test_skills_terms_may_match_in_different_columns(db_session):
    """Columns are OR-ed per term: one word in the title, the other in the description."""
    _skill(db_session, "pdf-utils", "PDF Utilities", "Extract tables from documents.")
    assert _slugs(us.search_skills_group(db_session, "pdf extract", 10)) == {"pdf-utils"}


def test_skills_term_may_match_the_category_column(db_session):
    """The skills group searches category too — that column must be term-aware as well."""
    _skill(db_session, "lint-tool", "Lint Tool", "Checks style.", category="devops")
    assert _slugs(us.search_skills_group(db_session, "lint devops", 10)) == {"lint-tool"}


def test_skills_three_word_query_requires_all_three(db_session):
    _skill(db_session, "hit", "PDF tools", "Extract every table.")
    _skill(db_session, "miss", "PDF tools", "Extract every image.")
    assert _slugs(us.search_skills_group(db_session, "pdf table extract", 10)) == {"hit"}


def test_skills_word_order_is_irrelevant(db_session):
    _skill(db_session, "csv-toolkit", "CSV Toolkit", "Cleanup helper.")
    assert _slugs(us.search_skills_group(db_session, "cleanup csv", 10)) == {"csv-toolkit"}


def test_skills_repeated_whitespace_does_not_create_an_empty_term(db_session):
    _skill(db_session, "hit", "CSV", "cleanup")
    _skill(db_session, "miss", "CSV", "convert")
    assert _slugs(us.search_skills_group(db_session, "csv   cleanup", 10)) == {"hit"}


def test_skills_case_is_ignored_across_all_terms(db_session):
    _skill(db_session, "csv-toolkit", "csv toolkit", "cleanup helper")
    assert _slugs(us.search_skills_group(db_session, "CSV CleanUp", 10)) == {"csv-toolkit"}


def test_loops_group_is_multi_term(db_session):
    _verifier(db_session, "tdd-loop", "TDD Loop", "Runs until the suite is green.")
    assert _slugs(us.search_loops_group(db_session, "tdd green", 10)) == {"tdd-loop"}
    assert us.search_loops_group(db_session, "tdd purple", 10) == []


def test_bundles_group_is_multi_term(db_session):
    _bundle(db_session, "tdd-bundle", "TDD Bundle", "A cookbook of testing skills.")
    assert _slugs(us.search_bundles_group(db_session, "tdd testing", 10)) == {"tdd-bundle"}
    assert us.search_bundles_group(db_session, "tdd cooking", 10) == []


def test_personalities_group_is_multi_term(db_session):
    _personality(db_session, "ruthless", "Ruthless Mentor", "Stress-tests every plan.")
    assert _slugs(us.search_personalities_group(db_session, "mentor plan", 10)) == {"ruthless"}
    assert us.search_personalities_group(db_session, "mentor holiday", 10) == []


def test_connectors_group_is_multi_term(db_session):
    _connector(db_session, "slack-notify", "Slack Notify")
    assert _slugs(us.search_connectors_group(db_session, "slack notify", 10)) == {"slack-notify"}
    assert us.search_connectors_group(db_session, "slack archive", 10) == []


def test_federated_hub_leg_is_multi_term(db_session):
    """The #282-indexed hub table must AND terms over the concatenated blob."""
    _hub_skill(db_session, "csv-cleaner", "CSV Cleaner", "Tidies up messy exports.")
    _hub_skill(db_session, "csv-convert", "CSV Convert", "Turns sheets into JSON.")
    rows, status = us.search_federated_group(db_session, "csv tidies", 10)
    assert _slugs(rows) == {"csv-cleaner"}
    assert status == "warm"


def test_federated_terms_may_span_title_and_description(db_session):
    _hub_skill(db_session, "pdf-utils", "PDF Utilities", "Extract tables from documents.")
    rows, _ = us.search_federated_group(db_session, "pdf extract", 10)
    assert _slugs(rows) == {"pdf-utils"}


def test_federated_term_may_match_the_slug(db_session):
    """slug is part of the indexed blob, so a term may land there."""
    _hub_skill(db_session, "invoice-parser", "Document Reader", "Reads things.")
    rows, _ = us.search_federated_group(db_session, "invoice reads", 10)
    assert _slugs(rows) == {"invoice-parser"}


# ── no-regression: single-term, empty, and visibility are untouched ──────────


def test_single_term_substring_behaviour_unchanged(db_session):
    _skill(db_session, "csv-toolkit", "CSV Toolkit", "whatever")
    _skill(db_session, "reader", "Nothing", "reads a csv file")
    _skill(db_session, "json-only", "Nothing", "reads a json file")
    assert _slugs(us.search_skills_group(db_session, "csv", 10)) == {"csv-toolkit", "reader"}


def test_single_term_is_still_an_infix_match_not_word_anchored(db_session):
    """'lean' must still match 'cleanup' — we did not sneak in word-boundary matching."""
    _skill(db_session, "cleaner", "cleanup helper", "x")
    assert _slugs(us.search_skills_group(db_session, "lean", 10)) == {"cleaner"}


def test_empty_query_matches_everything_as_before(db_session):
    _skill(db_session, "a", "Alpha", "one")
    _skill(db_session, "b", "Beta", "two")
    assert _slugs(us.search_skills_group(db_session, "", 10)) == {"a", "b"}
    assert _slugs(us.search_skills_group(db_session, "   ", 10)) == {"a", "b"}


def test_multi_term_does_not_leak_private_or_archived_rows(db_session):
    """Widening the predicate must not widen VISIBILITY."""
    _skill(db_session, "public-hit", "CSV Toolkit", "cleanup helper")
    _skill(db_session, "private-hit", "CSV Toolkit", "cleanup helper", is_public=False)
    _skill(db_session, "archived-hit", "CSV Toolkit", "cleanup helper", is_archived=True)
    assert _slugs(us.search_skills_group(db_session, "csv cleanup", 10)) == {"public-hit"}


def test_limit_is_still_applied(db_session):
    for i in range(5):
        _skill(db_session, f"csv-{i}", f"CSV Tool {i}", "cleanup helper")
    assert len(us.search_skills_group(db_session, "csv cleanup", 3)) == 3


def test_exact_prefix_still_ranks_first(db_session):
    """Ordering contract survives the predicate change."""
    _skill(db_session, "zzz", "Zebra csv cleanup", "csv cleanup")
    _skill(db_session, "aaa", "csv cleanup tool", "x csv cleanup")
    assert [r["slug"] for r in us.search_skills_group(db_session, "csv cleanup", 10)][0] == "aaa"


# ── bounded cost ─────────────────────────────────────────────────────────────


def test_term_count_is_capped():
    """A pathological query must not build an unbounded AND-chain."""
    assert us._terms("a b c d e f g h i j k l m n o p") == ["a", "b", "c", "d", "e", "f"]
    assert len(us._terms("x " * 500)) <= us._MAX_TERMS


def test_terms_of_a_normal_query():
    assert us._terms("csv cleanup") == ["csv", "cleanup"]
    assert us._terms("  csv   cleanup  ") == ["csv", "cleanup"]
    assert us._terms("") == []


def test_match_terms_never_returns_none():
    """A null criterion would make filter() a silent no-op returning the whole table."""
    assert us._match_terms("", Skill.title) is not None
    assert us._match_terms("csv cleanup", Skill.title, Skill.description) is not None


def test_match_terms_rejects_a_columnless_call():
    with pytest.raises(ValueError):
        us._match_terms("csv")


# ── federated relevance ranking must understand multi-term matches ───────────


def test_federated_relevance_ranks_all_terms_present_above_no_match():
    both = us._federated_relevance(
        {"title": "CSV Toolkit", "description": "cleanup helper", "slug": "csv-toolkit"}, "csv cleanup"
    )
    neither = us._federated_relevance(
        {"title": "JSON Toolkit", "description": "converter", "slug": "json-toolkit"}, "csv cleanup"
    )
    assert both < neither


def test_federated_relevance_still_puts_exact_title_first():
    exact = us._federated_relevance({"title": "csv cleanup", "description": "", "slug": "a"}, "csv cleanup")
    partial = us._federated_relevance({"title": "CSV Toolkit", "description": "cleanup", "slug": "b"}, "csv cleanup")
    assert exact < partial


def test_federated_relevance_prefers_all_terms_in_title_over_spread_across_fields():
    in_title = us._federated_relevance({"title": "csv bulk cleanup", "description": "", "slug": "a"}, "csv cleanup")
    spread = us._federated_relevance({"title": "csv tool", "description": "cleanup", "slug": "b"}, "csv cleanup")
    assert in_title < spread


def test_federated_relevance_is_a_stable_sort_key():
    rows = [
        {"title": "zzz csv cleanup", "description": "", "slug": "z"},
        {"title": "csv cleanup", "description": "", "slug": "a"},
        {"title": "unrelated", "description": "", "slug": "u"},
    ]
    ordered = sorted(rows, key=lambda r: us._federated_relevance(r, "csv cleanup"))
    assert [r["slug"] for r in ordered] == ["a", "z", "u"]


# ── source-level contracts ───────────────────────────────────────────────────


def test_hub_predicate_uses_the_single_blob_expression_per_term():
    """Regression guard for issue #282: never OR three separate column ILIKEs."""
    src = inspect.getsource(us.search_federated_group)
    assert "_match_terms(q, _search_blob)" in src, (
        "hub filter must AND the terms over the SAME blob expression the GIN trigram "
        "index is built on — see alembic/versions/issue282_fed_hub_trgm.py"
    )


@pytest.mark.parametrize(
    "group",
    [
        "search_skills_group",
        "search_loops_group",
        "search_bundles_group",
        "search_personalities_group",
        "search_connectors_group",
        "search_federated_group",
    ],
)
def test_every_group_routes_through_the_shared_term_matcher(group):
    """No group may keep its own f"%{q}%" whole-string predicate."""
    src = inspect.getsource(getattr(us, group))
    assert "_match_terms(" in src, f"{group} does not use the shared multi-term matcher"


def test_federated_cache_leg_is_term_aware():
    """The in-Python first_page scan must use the same AND-terms semantic as the SQL legs."""
    src = inspect.getsource(us.search_federated_group)
    assert "all(term in haystack for term in ql_terms)" in src, (
        "the first_page cache scan still does a whole-string `q in field` check — it would "
        "disagree with the hub leg about what counts as a match"
    )
