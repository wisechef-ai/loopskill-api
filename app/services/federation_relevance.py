"""Relevance ordering for federated snapshot search.

fdeloop_0808 Phase B.

## Why this exists

``federation_adapters.HermesHubAdapter.search`` filtered correctly and then
ordered by ``title`` before applying ``LIMIT``. Against the live 90,605-row hub
snapshot, ``q=seo`` matches 676 rows and keeps 25 — the 25 that sort first
alphabetically. The row whose slug is literally ``seo`` sorts at position ~400
and is therefore **unreachable at any page size the UI uses**.

The downstream ranker (``metasearch.rank``) then re-orders those 25. It cannot
recover a row that SQL already discarded. That is the council's *"recall failure
masquerading as a sorting problem"*: **relevance must be applied before the
truncation, in the database.**

## Design

Two functions, one shared tier definition:

* ``relevance_tier`` — pure Python, the readable specification. Unit-tested.
* ``relevance_order_clauses`` — the same ladder as SQL ``CASE`` expressions,
  fed to ``order_by`` so the LIMIT keeps the most relevant rows.

Keeping both in one module is deliberate. When the two drift, search silently
returns something other than what the tests assert — so the ladder is defined
once, as data (``_TIERS``), and both surfaces are generated from it.

**No new ranker.** The plan is explicit: add a lexical term to the ordering, do
not write a second ranking system. ``metasearch.rank()`` keeps its job
(popularity percentile + curated boost across sources); this only decides which
rows survive the per-source cut, which is exactly where recall was being lost.

## Why LIKE and not full-text search

Postgres FTS would need a tsvector column, a GIN index, a migration, and a
reindex of 90k rows — and would change match SEMANTICS (stemming, stopwords)
in ways the current ILIKE predicate's callers do not expect. The recall bug is
in the ORDER BY, not the WHERE: the matching rows are already found. Ordering
those matches by where the term appears fixes the observed defect with no
schema change and no semantic drift. FTS is the right next step when ranking
WITHIN a tier starts to matter; it is not needed to stop discarding the answer.
"""

from __future__ import annotations

from typing import Any

# The ladder, most-relevant first. Each entry is (name, python_predicate,
# sql_builder). Position IS the tier number, so the two surfaces cannot
# disagree about ordering — only about a single predicate, which the tests pin.
#
# Rationale for the order:
#   exact slug      — the user typed an identifier; nothing beats it
#   slug prefix     — "seo" -> "seo-geo": still an identifier match
#   slug contains   — "seo" -> "ai-seo"
#   title prefix    — the human-facing name starts with the term
#   title contains
#   identifier      — the federated canonical id (owner/repo/path)
#   description     — weakest: the term appears in prose


def _norm(v: Any) -> str:
    return (v or "").strip().lower()


def slugify_query(q: str | None) -> str:
    """Normalise a human query into slug shape for the identifier tiers.

    Users type ``code review``; the identifier is ``code-review``. Without this,
    an exact identifier match is demoted to a title/description match and loses
    to any row that merely CONTAINS the words — measured on prod 2026-08-08,
    ``q="code review"`` put ``eb-code-review``, ``codereview-assistant`` and
    ``code-review-terry`` above ``code-review`` itself.

    Applied ONLY to the slug tiers. Title and description keep the raw query,
    because collapsing their whitespace would break phrase matching in prose.
    """
    return "-".join(_norm(q).split())


_TIERS: list[tuple[str, Any, Any]] = [
    # NOTE — there is deliberately NO separate "exact slug" tier.
    #
    # The first draft had one, ranked above slug_prefix. The Phase-B RED-proof
    # could not make any test redden when it was removed, which turned out to be
    # correct rather than a weak test: every exact match is ALSO a prefix match,
    # and among prefix matches the exact one is by definition the shortest — so
    # the `func.length(slug)` tiebreak in `relevance_order_clauses` already puts
    # it first, unconditionally. The tier was a redundant branch that could only
    # ever agree with the mechanism below it.
    #
    # Deleted rather than kept "for clarity": a branch that cannot change an
    # outcome is a branch that will eventually be edited on the false belief
    # that it does. If a future signal makes exact-vs-prefix genuinely distinct
    # (e.g. a popularity term that could outweigh length), re-add it WITH a test
    # that reddens when it is removed.
    (
        "slug_prefix",
        lambda q, s, t, d, i, sq: s.startswith(sq),
        lambda col, q, sq: col["slug"].ilike(f"{sq}%"),
    ),
    (
        "slug_contains",
        lambda q, s, t, d, i, sq: sq in s,
        lambda col, q, sq: col["slug"].ilike(f"%{sq}%"),
    ),
    (
        "title_prefix",
        lambda q, s, t, d, i, sq: t.startswith(q),
        lambda col, q, sq: col["title"].ilike(f"{q}%"),
    ),
    (
        "title_contains",
        lambda q, s, t, d, i, sq: q in t,
        lambda col, q, sq: col["title"].ilike(f"%{q}%"),
    ),
    (
        "identifier_contains",
        lambda q, s, t, d, i, sq: sq in i,
        lambda col, q, sq: col["identifier"].ilike(f"%{sq}%"),
    ),
    (
        "description_contains",
        lambda q, s, t, d, i, sq: q in d,
        lambda col, q, sq: col["description"].ilike(f"%{q}%"),
    ),
]

# Anything matching no tier sorts after every tier. It should not appear at all
# (the WHERE clause already filtered), but ordering must be total regardless —
# an ORDER BY with an unhandled case yields database-dependent output.
NO_MATCH_TIER = len(_TIERS)

# With no query every row is equally relevant.
#
# Note this equals tier 0 by construction, and that is not a coincidence: an
# empty string is a prefix of every slug, so WITHOUT the short-circuit in
# `relevance_tier` every row would fall into `slug_prefix` (0) anyway. The
# guard is a readability short-circuit, not a behaviour change — the Phase-B
# RED-proof proved this by failing to redden any test when it was removed.
#
# The guard that IS load-bearing is the one in `relevance_order_clauses`: it
# suppresses the ORDER BY term entirely, so a browse does not pay for a CASE
# expression that every row satisfies identically. That one is RED-proofed.
NEUTRAL_TIER = 0


def relevance_tier(
    query: str | None,
    *,
    slug: str = "",
    title: str = "",
    description: str = "",
    identifier: str = "",
) -> int:
    """Return the relevance tier of one row: LOWER is more relevant.

    The readable specification of the ordering. ``relevance_order_clauses``
    emits the same ladder as SQL; both are generated from ``_TIERS``.
    """
    q = _norm(query)
    if not q:
        return NEUTRAL_TIER
    sq = slugify_query(query)
    s, t, d, i = _norm(slug), _norm(title), _norm(description), _norm(identifier)
    for idx, (_name, py_pred, _sql) in enumerate(_TIERS):
        if py_pred(q, s, t, d, i, sq):
            return idx
    return NO_MATCH_TIER


def relevance_order_clauses(model: Any, query: str | None) -> list[Any]:
    """ORDER BY clauses that put the most relevant rows first, for use BEFORE
    ``.limit()``.

    Returns a list so the caller can append its own stable tiebreak. With an
    empty query the relevance term is omitted entirely and the caller's
    tiebreak stands alone — a browse, not a search.

    TWO terms, in order:

    1. **tier** — where the term appears (slug > title > identifier > prose).
    2. **slug length** — WITHIN a tier, the shortest slug wins.

    The second term is not cosmetic. Measured on prod 2026-08-08, ``q=polymark``
    put ``polymarket-worldcup-group-repricer``, ``polymarket-manual-trade`` and
    ``polymarket-markets`` above plain ``polymarket``: all four share the
    ``slug_prefix`` tier, so the alphabetical tiebreak decided, and it decided
    badly. Shortest-slug-first encodes the obvious intent — a prefix query most
    likely wants the row that is closest to being the term itself.
    """
    from sqlalchemy import case, func

    q = _norm(query)
    if not q:
        return []
    sq = slugify_query(query)

    cols = {
        "slug": model.slug,
        "title": model.title,
        "description": model.description,
        "identifier": model.identifier,
    }
    # Every tier now uses ilike, which is already case-insensitive on both
    # sides — the func.lower() special-case existed only for the deleted
    # exact-slug `==` comparison.
    whens = []
    for idx, (_name, _py, sql_builder) in enumerate(_TIERS):
        whens.append((sql_builder(cols, q, sq), idx))

    return [case(*whens, else_=NO_MATCH_TIER), func.length(model.slug)]
