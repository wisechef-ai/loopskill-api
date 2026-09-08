"""loopskill_search — catalog search: native first, federation appended.

Backed by the same ORM query used by ``GET /api/skills/search``. We import the
SQLAlchemy primitives directly rather than calling the FastAPI handler to keep
the MCP path free of HTTP loopback.

Issue #111: when the literal ILIKE pass returns fewer than
``HYBRID_MIN_KEYWORD_HITS`` results AND the caller supplied a non-empty query,
augment with ``recall_skills`` (BM25 + optional vector). This closes the
"recall finds many, search finds zero" gap reported on a broad
multi-keyword dev query.

unisearch_0709 P2 — the federated append, and the honest guarantee it carries
-----------------------------------------------------------------------------
Verified live 2026-09-08: this tool returned ``{"results": [], "total": 0}`` for
``"graft"`` while the metasearch fan-out found ``skills-sh:trailhq--graft--graft``
and ``loopskill_install`` installed it fine. Discovery disagreed with install, so
every MCP-facing agent concluded LoopSkill has nothing on any federated topic.

So the native pass now runs EXACTLY as before, and federated rows are APPENDED
after it from the shared metasearch cache. What that is and is not:

  **It is best-effort, from a cache. It is NOT a live, guaranteed fan-out.**

This tool never fans out on the caller's thread — a cold fan-out was measured at
>90s on prod, and an agent waiting 90s on a search reports the platform as
broken. It performs one cache-only read (``metasearch_cache.get_entry``) and
reports what it found through the ``federated`` key:

  ``fresh``    — cached result within TTL.
  ``stale``    — past TTL, inside the grace window; served as-is.
  ``cold``     — nothing cached on this worker. Native results only. Not an
                 error, and not a claim that federation has nothing: warm the
                 query through ``GET /api/skills/metasearch`` and ask again.
  ``degraded`` — the shared cache tier is unreachable, so freshness cannot be
                 confirmed fleet-wide.

Federated rows are COMPACT (``slug``, ``title``, ``install_ref``,
``deployable``, ``install_path``, ``origin_url``, ``quality``) — full bodies
stay behind ``loopskill_install``, which the ``install_ref`` feeds directly.
``deployable`` is re-derived from the source's own install-router verdict, never
trusted from the cache, so a deep-link/non-redistributable row can never present
an install affordance it cannot honour.

The four pre-P2 keys (``results``, ``total``, ``backend``, ``hybrid_augmented``)
keep their exact meaning and type; ``total`` is still the NATIVE total.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models import Skill
from app.routes import _install_counts_for, _skill_to_out
from app.services.mcp_federated_search import federated_append

logger = logging.getLogger(__name__)

# Threshold below which we widen the search via hybrid recall.
HYBRID_MIN_KEYWORD_HITS = 3


def loopskill_search(
    db: Session,
    query: str | None = None,
    category: str | None = None,
    tier: str | None = None,
    limit: int = 100,
    hybrid: bool = True,
    federated_limit: int | None = None,
    api_key_id: Any | None = None,
) -> dict[str, Any]:
    """Search the public catalog by keyword, then append cached federated rows.

    Returns ``{"results": [...], "total": int, "backend": str,
    "hybrid_augmented": bool, "federated": str}``.

    - ``backend = "keyword"`` — literal ILIKE pass alone.
    - ``backend = "hybrid"``  — literal + recall results unioned.
    - ``backend = "recall_only"`` — literal returned zero, recall provided all.
    - ``federated`` — ``fresh`` | ``stale`` | ``cold`` | ``degraded``. Federation
      is best-effort from a SHARED CACHE, never a live guaranteed fan-out; see
      the module docstring. ``total`` counts NATIVE rows only.

    ``federated_limit`` caps the append (None/omitted → 10, hard maximum 30) —
    a context-window budget, not a relevance judgement.

    WIS-948: default limit raised 20->100 so a bare loopskill_search() call
    returns the full (or near-full) catalog instead of silently capping at 20.
    The HTTP search endpoint also honours a ?limit= alias for the same reason:
    Pro-tier buyers browsing the catalog saw only 20/63 paid skills they own.
    The MCP WIRE default stays 20 (app/mcp/server.py) — the two differ on
    purpose: an agent's context window is not a browser's scroll position.
    """
    # Public-scope MCP tool: searches the public skill catalog only; is_public filter applied internally.
    q = (
        db.query(Skill)
        .options(
            joinedload(Skill.versions),
            joinedload(Skill.creator),
        )
        .filter(
            Skill.is_public == True,  # noqa: E712
            Skill.is_archived == False,  # noqa: E712
        )
    )

    if query:
        q = q.filter((Skill.title.ilike(f"%{query}%")) | (Skill.description.ilike(f"%{query}%")))
    if category:
        q = q.filter(Skill.category == category)
    if tier:
        q = q.filter(Skill.tier == tier)

    q = q.order_by(Skill.updated_at.desc())
    total = q.count()
    capped_limit = max(1, min(limit, 100))
    rows = q.limit(capped_limit).all()

    counts = _install_counts_for(db, [s.id for s in rows])
    keyword_results = [_skill_to_out(s, *counts.get(s.id, (0, 0))).model_dump(mode="json") for s in rows]

    backend = "keyword"
    augmented = False
    final_results = keyword_results
    final_total = total

    if hybrid and query and len(rows) < HYBRID_MIN_KEYWORD_HITS:
        try:
            from app.recall_routes import recall_skills

            tier_for_recall = [tier] if tier else ["free", "pro", "pro_plus"]  # canonical slugs (Phase G)
            recall_blob = recall_skills(
                db,
                query=query,
                tier_filter=tier_for_recall,
                limit=max(capped_limit, 10),
                user_id=None,
                is_master=True,
                user_tier=None,
            )
            recall_hits = recall_blob.get("hits", []) if isinstance(recall_blob, dict) else []

            existing_slugs = {s.slug for s in rows}
            extra_slugs = [
                h["slug"]
                for h in recall_hits
                if isinstance(h, dict) and h.get("slug") and h["slug"] not in existing_slugs
            ]

            if extra_slugs:
                extra_q = (
                    db.query(Skill)
                    .options(
                        joinedload(Skill.versions),
                        joinedload(Skill.creator),
                    )
                    .filter(
                        Skill.is_public == True,  # noqa: E712
                        Skill.is_archived == False,  # noqa: E712
                        Skill.slug.in_(extra_slugs),
                    )
                )
                if category:
                    extra_q = extra_q.filter(Skill.category == category)
                extra_rows = {sk.slug: sk for sk in extra_q.all()}
                ordered_extras = [extra_rows[s] for s in extra_slugs if s in extra_rows]

                if ordered_extras:
                    extra_counts = _install_counts_for(db, [s.id for s in ordered_extras])
                    extra_results = [
                        _skill_to_out(s, *extra_counts.get(s.id, (0, 0))).model_dump(mode="json")
                        for s in ordered_extras
                    ]
                    final_results = (keyword_results + extra_results)[:capped_limit]
                    final_total = total + len(extra_results)
                    augmented = True
                    backend = "recall_only" if not rows else "hybrid"
        # Rationale: hybrid recall failure must never kill the keyword-only search path
        except Exception:  # noqa: BLE001
            logger.exception("loopskill_search hybrid fallback failed; returning keyword only")

    # ── unisearch_0709 P2: federated append (cache ONLY, never a fan-out) ───
    # Native/curated rows are ALWAYS first; federated rows are appended after
    # them and never interleaved. `total` is deliberately NOT incremented — it
    # is an existing key with an existing meaning (the native match count), and
    # silently redefining it would break every caller that reads it.
    fed_rows, fed_state = federated_append(
        query,
        limit=federated_limit,
        exclude_slugs={r.get("slug") for r in final_results if r.get("slug")},
    )
    _record_federated_demand(
        db, query, native_hits=len(final_results), rows=fed_rows, freshness=fed_state, api_key_id=api_key_id
    )

    return {
        "results": final_results + fed_rows,
        "total": final_total,
        "backend": backend,
        "hybrid_augmented": augmented,
        "federated": fed_state,
    }


def _record_federated_demand(
    db: Session,
    query: str | None,
    *,
    native_hits: int,
    rows: list[dict[str, Any]],
    freshness: str,
    api_key_id: Any | None,
) -> None:
    """A query WE could not answer but federation could is a demand signal.

    It goes to its OWN store (``demand_capture.record_federated_fulfilled_query``
    -> a ``federated_fulfilled_queries`` telemetry event), never to
    ``missing_skill_queries``: that table means zero TOTAL results, and logging a
    fulfilled query into it would inflate the catalog-gap count the demand brief
    is built from. See that function's comment block for the store decision.
    """
    if native_hits or not rows:
        return
    from app.services.demand_capture import record_federated_fulfilled_query

    record_federated_fulfilled_query(db, query, rows=rows, freshness=freshness, api_key_id=api_key_id)
