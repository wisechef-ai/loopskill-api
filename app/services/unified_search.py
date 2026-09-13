"""Unified cross-type search helpers — powers GET /api/search.

feat/unified-search: a single anonymous call that searches skills, loops
(verifiers), bundles, personalities, and connectors and returns them grouped
by type ("Spotify-style" search). Each per-type query below deliberately
COPIES the public-visibility filter expression from that type's existing
public route so the two surfaces (the dedicated per-type browse/search
endpoint and this unified endpoint) can never disagree about what's publicly
visible:

  * skills        -> app/skill_routes.py:search_skills          (Skill.is_public == True, Skill.is_archived == False)
  * loops         -> app/verifier_routes.py:list_verifiers      (Verifier.is_public.is_(True), Verifier.is_archived.is_(False))
  * bundles       -> app/bundle_routes.py:discover_cookbooks    (Bundle.visibility == "public", Bundle.slug.isnot(None))
  * personalities -> app/personality_routes.py:list_personalities (Personality.is_public.is_(True), Personality.is_archived.is_(False))
  * connectors    -> app/connector_routes.py:browse_connectors  (Connector.is_public.is_(True), Connector.is_archived.is_(False))

mesh0408 T1-D scope note: this is FIVE groups, not seven. ``verifiers`` (as a
type distinct from ``loops`` — Verifier already backs the loops group above)
and ``composite_loops`` have NO unified-search coverage and are not built by
this module. That is a recorded, known gap — see hub.md §3 — not an oversight
to silently patch over.

SEARCH SEMANTICS: case-insensitive substring (ILIKE) on name/title +
description, no new search infra (no tsvector, no embeddings) in this module.
Queries are TOKENISED on whitespace: every term must match (AND), and a term
may match in any of that group's searchable columns (OR). See ``_match_terms``.

  Before (2026-09-13 and earlier): one ILIKE over the WHOLE query string, so
  ``q=csv cleanup`` only matched rows containing that exact adjacent phrase.
  Measured on production that morning: ``q=csv`` -> 20 federated hits,
  ``q=cleanup`` -> 20, ``q=csv cleanup`` -> 0; ``q=pdf`` -> 3 skills,
  ``q=pdf extract`` -> 0. The entire multi-word query space returned nothing.
  Pinned by tests/test_unified_search_multiword.py.

Single-term and empty queries keep byte-identical behaviour: one term builds
exactly the old OR-of-columns predicate, and an empty query still builds the
old ``ILIKE '%%'`` match-everything chain.

Deterministic ordering: exact-prefix title/name matches first, then a per-type
"popularity" signal where cheaply available (install_count / run_count), then
alphabetical as the final tiebreaker so results are stable across runs. Phrase
adjacency remains an ORDERING signal (an exact phrase hit still ranks above a
scattered one) — it is no longer a FILTER.

PERF: one SELECT per type with LIMIT applied in SQL (no Python-side slicing
of an unbounded result set), no per-row lazy loads — only the columns each
card needs are read off the ORM objects returned by the single query. Bundles
carries one small additional aggregate query (grouped skill counts for the
already-limited result rows) to expose ``skill_count`` without N+1 (i.e. not
one count query per bundle row).
"""

from __future__ import annotations

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import Bundle, BundleSkill, Connector, Personality, Skill, Verifier

_DESC_TRUNCATE = 200

# Upper bound on tokenised query terms. Each term adds one AND-ed OR-group to
# the predicate, so an unbounded term count would let a pathological query
# build an arbitrarily large WHERE clause on a per-keystroke endpoint. Six is
# comfortably above any real search-box query ("pdf table extract to csv" is
# five) and keeps the worst case cheap.
_MAX_TERMS = 6


def _terms(q: str) -> list[str]:
    """Tokenise a query into at most ``_MAX_TERMS`` non-empty whitespace terms."""
    return (q or "").split()[:_MAX_TERMS]


def _match_terms(q: str, *columns) -> ColumnElement[bool]:
    """AND across query terms, OR across ``columns`` — the search-box semantic.

    Every term must match somewhere; a single term may match in any column.
    ``_match_terms("csv cleanup", title, description)`` becomes::

        (title ILIKE '%csv%' OR description ILIKE '%csv%')
        AND (title ILIKE '%cleanup%' OR description ILIKE '%cleanup%')

    Back-compat is exact, not approximate:
      * ONE term collapses to the old single OR-of-columns predicate;
      * an EMPTY query collapses to the old ``ILIKE '%%'`` chain that matched
        every row with at least one non-null searchable column.
    So this widens multi-word queries without moving single-word or empty ones.

    Never returns None: ``_terms(q) or [""]`` always yields at least one term,
    and every caller passes at least one column — so ``filter()`` can never be
    handed a null criterion (which SQLAlchemy would silently treat as no-op,
    i.e. return the entire table).
    """
    if not columns:
        raise ValueError("_match_terms needs at least one column to search")
    return and_(*(or_(*(column.ilike(f"%{term}%") for column in columns)) for term in _terms(q) or [""]))


def _truncate(text: str | None) -> str | None:
    """Truncate a description to ~200 chars, matching the compact-card contract."""
    if not text:
        return text
    text = text.strip()
    if len(text) <= _DESC_TRUNCATE:
        return text
    return text[:_DESC_TRUNCATE].rstrip() + "…"


def _federated_relevance(row: dict, q: str) -> tuple[int, str, str]:
    """Return the explainable, stable ranking key for a cached federated row.

    Multi-term aware (2026-09-13): with tokenised matching, a row can be a
    legitimate hit without containing the query as an adjacent phrase, so a
    purely phrase-based key collapsed every such row into the same worst
    bucket. Phrase adjacency is still the STRONGEST signal (buckets 0-3) — it
    just no longer decides membership. Rows matching all terms scattered
    across fields (buckets 4-6) now sort above rows matching none (7).
    """
    query = q.casefold()
    title = str(row.get("title") or "").casefold()
    description = str(row.get("description") or "").casefold()
    slug = str(row.get("slug") or "").casefold()
    terms = [t.casefold() for t in _terms(q)]
    if title == query:
        bucket = 0
    elif title.startswith(query):
        bucket = 1
    elif query in title:
        bucket = 2
    elif query in description:
        bucket = 3
    elif terms and all(t in title for t in terms):
        bucket = 4
    elif terms and all(t in f"{title} {description}" for t in terms):
        bucket = 5
    elif terms and all(t in f"{title} {description} {slug}" for t in terms):
        bucket = 6
    else:
        bucket = 7
    return bucket, title, slug


def search_skills_group(db: Session, q: str, limit: int) -> list[dict]:
    """Public skills matching ``q``, newest surface: /api/skills/search twin."""
    prefix_like = f"{q}%"
    # Visibility filter copied verbatim from app/skill_routes.py:search_skills.
    query = db.query(Skill).filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
    query = query.filter(_match_terms(q, Skill.title, Skill.description, Skill.category))
    exact_prefix = case((Skill.title.ilike(prefix_like), 0), else_=1)
    query = query.order_by(exact_prefix, Skill.install_count.desc(), Skill.title.asc())
    rows = query.limit(limit).all()
    return [
        {
            "slug": s.slug,
            "title": s.title,
            "description": _truncate(s.description),
            "category": s.category,
            "tier": s.tier,
        }
        for s in rows
    ]


def search_loops_group(db: Session, q: str, limit: int) -> list[dict]:
    """Public loops (verifiers) matching ``q``: /api/loops (/api/verifiers) twin."""
    prefix_like = f"{q}%"
    # Visibility filter copied verbatim from app/verifier_routes.py:list_verifiers.
    query = db.query(Verifier).filter(Verifier.is_public.is_(True), Verifier.is_archived.is_(False))
    query = query.filter(_match_terms(q, Verifier.title, Verifier.description))
    exact_prefix = case((Verifier.title.ilike(prefix_like), 0), else_=1)
    query = query.order_by(exact_prefix, Verifier.run_count.desc(), Verifier.title.asc())
    rows = query.limit(limit).all()
    return [
        {
            "slug": v.slug,
            "title": v.title,
            "description": _truncate(v.description),
            "max_turns": v.max_turns,
            "tool_count": len(v.tool_allowlist or []),
            "run_count": v.run_count or 0,
        }
        for v in rows
    ]


def search_bundles_group(db: Session, q: str, limit: int) -> list[dict]:
    """Public bundles matching ``q``: /api/bundles/public (discover) twin."""
    prefix_like = f"{q}%"
    # Visibility filter copied verbatim from app/bundle_routes.py:discover_cookbooks.
    query = db.query(Bundle).filter(Bundle.visibility == "public", Bundle.slug.isnot(None))
    query = query.filter(_match_terms(q, Bundle.name, Bundle.description))
    exact_prefix = case((Bundle.name.ilike(prefix_like), 0), else_=1)
    query = query.order_by(exact_prefix, Bundle.name.asc())
    rows = query.limit(limit).all()
    if not rows:
        return []

    # One grouped aggregate query for skill_count across all limited rows —
    # avoids a per-bundle COUNT (N+1) while still surfacing the extra.
    bundle_ids = [b.id for b in rows]
    count_rows = (
        db.query(BundleSkill.bundle_id, func.count(BundleSkill.skill_id))
        .filter(BundleSkill.bundle_id.in_(bundle_ids), BundleSkill.source != "disabled")
        .group_by(BundleSkill.bundle_id)
        .all()
    )
    counts = {bid: cnt for bid, cnt in count_rows}

    return [
        {
            "slug": b.slug,
            "name": b.name,
            "description": _truncate(b.description),
            "skill_count": counts.get(b.id, 0),
        }
        for b in rows
    ]


def search_personalities_group(db: Session, q: str, limit: int) -> list[dict]:
    """Public personalities matching ``q``: /api/personalities twin."""
    prefix_like = f"{q}%"
    # Visibility filter copied verbatim from app/personality_routes.py:list_personalities.
    query = db.query(Personality).filter(Personality.is_public.is_(True), Personality.is_archived.is_(False))
    query = query.filter(_match_terms(q, Personality.title, Personality.description))
    exact_prefix = case((Personality.title.ilike(prefix_like), 0), else_=1)
    query = query.order_by(exact_prefix, Personality.install_count.desc(), Personality.title.asc())
    rows = query.limit(limit).all()
    return [
        {
            "slug": p.slug,
            "title": p.title,
            "description": _truncate(p.description),
            "category": p.category,
            "tier": p.tier,
        }
        for p in rows
    ]


def search_connectors_group(db: Session, q: str, limit: int) -> list[dict]:
    """Public connectors matching ``q``: /api/connectors twin.

    mesh0408 T1-D: fifth group added alongside skills/loops/bundles/
    personalities. T1-C (sister phase) populates the underlying ``connectors``
    table; an empty table is a CORRECT response here — this group's contract
    is a well-formed (possibly empty) list, matching the other four groups'
    "no invented data for an empty group" rule.
    """
    prefix_like = f"{q}%"
    # Visibility filter copied verbatim from app/connector_routes.py:browse_connectors.
    query = db.query(Connector).filter(Connector.is_public.is_(True), Connector.is_archived.is_(False))
    query = query.filter(_match_terms(q, Connector.slug, Connector.title))
    exact_prefix = case((Connector.title.ilike(prefix_like), 0), else_=1)
    query = query.order_by(exact_prefix, Connector.install_count.desc(), Connector.title.asc())
    rows = query.limit(limit).all()
    return [
        {
            "slug": c.slug,
            "title": c.title,
            "description": _truncate(c.description),
            "connector_type": c.connector_type,
        }
        for c in rows
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Issue #277 Fix B — the federated group + the pointer-visibility contract.
# ─────────────────────────────────────────────────────────────────────────────


def search_federated_group(db: Session, q: str, limit: int) -> tuple[list[dict], str]:
    """Federated skills matching ``q`` — CACHE-ONLY, never a live fan-out.

    Why cache-only (decision, design council 2026-08-25): ``/api/search`` fires
    per keystroke from the portal browse surface and is anonymous; the prod box
    shares ONE 60/hr GitHub budget across all users. A live fan-out here is the
    known incident class (superset_0606 Phase F). The reindex cron owns cache
    freshness; this read path never writes it.

    Search surface, in order:
      1. ``federation_hub_skills`` — the hub snapshot table (slug/title/
         description ILIKE, bounded). Primary because it is the only
         row-per-skill indexed store with titles.
      2. ``federation_index_cache.first_page`` rows for sources with no hub
         presence — JSON scan in Python, capped per source.

    Visibility contract (issue #277 break #2, RESOLVED BY DOCUMENTATION):
    materialized pointer ``Skill`` rows (``ext:source:slug``) are PRIVATE BY
    DESIGN — they are per-bundle install artifacts, not catalog entries.
    They must NEVER appear in the ``skills`` group (its is_public filter is
    correct), and this function is the ONLY sanctioned federated search
    surface. Do not "fix" visibility by flipping is_public on pointers.

    Returns ``(rows, cache_status)`` where cache_status is ``"warm"`` when any
    federated source had data to search, ``"cold"`` when both stores were
    empty (so the portal can distinguish "no matches" from "index
    unavailable" instead of rendering a silently empty section).
    """
    like = f"%{q}%"
    rows: list[dict] = []
    saw_data = False

    # 1. hub snapshot table
    from app.models import FederationHubSkill

    # issue #282: filter on ONE expression matching the migration's GIN
    # trigram index exactly (coalesce/concat of title+slug+description) —
    # NOT three independent .ilike() clauses OR'd together. Verified against
    # a 90k-row Postgres instance that the three-clause OR form makes the
    # planner price the resulting BitmapOr plan above a plain sequential
    # scan and silently fall back to it (812ms, unindexed). The single
    # concatenated expression is what the migration's index is built on, so
    # Postgres recognizes and uses it (0.1-15ms, index scan). See
    # alembic/versions/issue282_fed_hub_trgm.py and
    # tests/migrations/test_issue282_fed_hub_trgm.py for the measured proof.
    _search_blob = (
        func.coalesce(FederationHubSkill.title, "")
        + " "
        + func.coalesce(FederationHubSkill.slug, "")
        + " "
        + func.coalesce(FederationHubSkill.description, "")
    )
    hub_rows = (
        db.query(FederationHubSkill)
        # Multi-term (2026-09-13): one ILIKE per term over the SAME concatenated
        # blob, AND-ed. Each term is still an independent trigram probe against
        # the index above, so the plan stays an index scan — what #282 forbids
        # is OR-ing three DIFFERENT column expressions, not AND-ing several
        # patterns against the one indexed expression.
        .filter(_match_terms(q, _search_blob))
        .order_by(
            case(
                (FederationHubSkill.title.ilike(q), 0),
                (FederationHubSkill.title.ilike(f"{q}%"), 1),
                (FederationHubSkill.title.ilike(like), 2),
                (FederationHubSkill.description.ilike(like), 3),
                else_=4,
            ),
            func.lower(FederationHubSkill.title).asc(),
            func.lower(FederationHubSkill.slug).asc(),
        )
        .limit(limit)
        .all()
    )
    if db.query(FederationHubSkill.id).limit(1).first() is not None:
        saw_data = True
    for r in hub_rows:
        origin = r.origin_url or ""
        if origin and not origin.lower().startswith(("http://", "https://")):
            origin = ""  # upstream-controlled scheme — never hand back a link
        rows.append(
            {
                "slug": r.slug,
                "title": (r.title or "").strip() or r.slug,
                "description": _truncate(r.description),
                "source": r.source or "hermes-hub",
                "install_ref": f"{r.source or 'hermes-hub'}:{r.slug}",
                "origin_url": origin,
                "deployable": False,
            }
        )

    # 2. first_page cache rows (bounded JSON scan). codex review (#277,
    # findings 5+6):
    #   * bulk-load ALL cached first pages in ONE query — a db.get() per
    #     source was 29 SQL statements for one zero-result search on a
    #     per-keystroke endpoint.
    #   * hermes-hub is excluded from the cache scan ONLY when the hub table
    #     has usable rows. A populated first_page with an empty hub table is a
    #     real, searchable state (hub snapshot lag) and must read warm.
    if len(rows) < limit:
        from app.models import FederationIndexCache
        from app.services.federation_sources_config import adapter_source_ids, github_tap_rows

        sources = set(adapter_source_ids()) | {str(r["source_id"]) for r in github_tap_rows()}
        if not saw_data:
            sources.add("hermes-hub")
        cache_rows = {
            c.source: c.first_page
            for c in db.query(FederationIndexCache).filter(FederationIndexCache.source.in_(sources)).all()
            if isinstance(c.first_page, list)
        }
        ql_terms = [t.lower() for t in _terms(q)] or [""]
        for source in sorted(sources):
            page = cache_rows.get(source) or []
            if page:
                saw_data = True
            for row in page:
                if not isinstance(row, dict):
                    continue
                title = str(row.get("title") or row.get("slug") or "")
                desc = str(row.get("description") or "")
                slug = str(row.get("slug") or "")
                # Same AND-terms/OR-fields semantic as the SQL groups, so the
                # cache leg and the hub leg can never disagree about what is a
                # match (they are merged into one federated list below).
                haystack = f"{title}\n{desc}\n{slug}".lower()
                if all(term in haystack for term in ql_terms):
                    origin = str(row.get("origin_url") or "")
                    if origin and not origin.lower().startswith(("http://", "https://")):
                        origin = ""
                    rows.append(
                        {
                            "slug": slug,
                            "title": title or slug,
                            "description": _truncate(desc or None),
                            "source": source,
                            "install_ref": f"{source}:{slug}",
                            "origin_url": origin,
                            "deployable": bool(row.get("install_path") == "fetch_origin"),
                        }
                    )

    rows.sort(key=lambda row: _federated_relevance(row, q))
    return rows[:limit], ("warm" if saw_data else "cold")
