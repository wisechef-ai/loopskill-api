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

SEARCH SEMANTICS: mirrors the existing per-type search — case-insensitive
substring (ILIKE) on name/title + description, no new search infra (no
tsvector, no embeddings) in this module. Deterministic ordering: exact-prefix
title/name matches first, then a per-type "popularity" signal where cheaply
available (install_count / run_count), then alphabetical as the final
tiebreaker so results are stable across runs.

PERF: one SELECT per type with LIMIT applied in SQL (no Python-side slicing
of an unbounded result set), no per-row lazy loads — only the columns each
card needs are read off the ORM objects returned by the single query. Bundles
carries one small additional aggregate query (grouped skill counts for the
already-limited result rows) to expose ``skill_count`` without N+1 (i.e. not
one count query per bundle row).
"""

from __future__ import annotations

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models import Bundle, BundleSkill, Connector, Personality, Skill, Verifier

_DESC_TRUNCATE = 200


def _truncate(text: str | None) -> str | None:
    """Truncate a description to ~200 chars, matching the compact-card contract."""
    if not text:
        return text
    text = text.strip()
    if len(text) <= _DESC_TRUNCATE:
        return text
    return text[:_DESC_TRUNCATE].rstrip() + "…"


def search_skills_group(db: Session, q: str, limit: int) -> list[dict]:
    """Public skills matching ``q``, newest surface: /api/skills/search twin."""
    like = f"%{q}%"
    prefix_like = f"{q}%"
    # Visibility filter copied verbatim from app/skill_routes.py:search_skills.
    query = db.query(Skill).filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
    query = query.filter(Skill.title.ilike(like) | Skill.description.ilike(like) | Skill.category.ilike(like))
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
    like = f"%{q}%"
    prefix_like = f"{q}%"
    # Visibility filter copied verbatim from app/verifier_routes.py:list_verifiers.
    query = db.query(Verifier).filter(Verifier.is_public.is_(True), Verifier.is_archived.is_(False))
    query = query.filter(Verifier.title.ilike(like) | Verifier.description.ilike(like))
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
    like = f"%{q}%"
    prefix_like = f"{q}%"
    # Visibility filter copied verbatim from app/bundle_routes.py:discover_cookbooks.
    query = db.query(Bundle).filter(Bundle.visibility == "public", Bundle.slug.isnot(None))
    query = query.filter(Bundle.name.ilike(like) | Bundle.description.ilike(like))
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
    like = f"%{q}%"
    prefix_like = f"{q}%"
    # Visibility filter copied verbatim from app/personality_routes.py:list_personalities.
    query = db.query(Personality).filter(Personality.is_public.is_(True), Personality.is_archived.is_(False))
    query = query.filter(Personality.title.ilike(like) | Personality.description.ilike(like))
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
    like = f"%{q}%"
    prefix_like = f"{q}%"
    # Visibility filter copied verbatim from app/connector_routes.py:browse_connectors.
    query = db.query(Connector).filter(Connector.is_public.is_(True), Connector.is_archived.is_(False))
    query = query.filter(Connector.slug.ilike(like) | Connector.title.ilike(like))
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
    DESIGN — they are per-cookbook install artifacts, not catalog entries.
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
        .filter(_search_blob.ilike(like))
        .order_by(FederationHubSkill.title.asc())
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
        ql = q.lower()
        for source in sorted(sources):
            if len(rows) >= limit:
                break
            page = cache_rows.get(source) or []
            if page:
                saw_data = True
            for row in page:
                if not isinstance(row, dict):
                    continue
                title = str(row.get("title") or row.get("slug") or "")
                desc = str(row.get("description") or "")
                slug = str(row.get("slug") or "")
                if ql in title.lower() or ql in desc.lower() or ql in slug.lower():
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
                    if len(rows) >= limit:
                        break

    return rows[:limit], ("warm" if saw_data else "cold")
