"""bundles0811 P3.6 — filters ("saved views") over the federated index.

Lock #9 (plan §0): with ~154,000 indexed skills the scarce resource is
ATTENTION, not supply. A user needs to carve that index into something
usable by source, license, trust_level, and tag — and the result of that
carve must be directly actionable: feedable into the bulk-add endpoint
(``POST /api/bundles/{id}/skills/bulk``, ``app/bundle_routes.py``) without a
client-side reshape.

DESIGN DECISION — query-string contract, NOT a persisted "saved view" table:

  The phase's own gate says "carve the index into something usable", and asks
  us to decide whether persistence is needed or a shareable filter query
  string suffices, picking the SIMPLER option. A saved-view TABLE would need:
  ownership, an authz predicate, a CRUD surface, and a migration — for a
  capability a URL query string already provides for free. Every filter this
  route accepts (source, license, trust_level, tag, q) round-trips through
  ordinary GET query parameters, so "saving" a view is just bookmarking or
  copy-pasting the URL — already true of every other browse/discover route in
  this codebase (``GET /api/bundles/discover``, ``GET /api/skills/search``,
  etc. — none of THEM persist either). Zero new tables, zero new authz
  surface, and the filter is trivially shareable (paste the URL) which is the
  actual product need name-checked in the gate ("carve ... into something
  usable" — usable BY a human deciding what to bulk-add, not a database row).

  If a genuine cross-session "pin this filter to my account" need shows up
  later (distinct from "share this URL"), it is a small additive table with
  no coupling to this route's contract — this module's response shape does
  not change either way.

Result → bulk-add contract: every row in ``results`` carries
``federated_source`` + ``federated_slug`` — copy the ``results`` array
verbatim (or map it) into ``POST .../skills/bulk``'s ``items`` list. See
``docs/`` / the PR body for a worked example; ``test_federation_filter_routes.py``
asserts this shape is bulk-add-consumable end-to-end (not just independently
correct).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import String, cast
from sqlalchemy.orm import Session

from app.database import get_db
from fastapi import Depends
from app.models import FederationHubSkill

router = APIRouter(tags=["federation", "filters"])

_MAX_PAGE_SIZE = 200
_MAX_TOTAL_SCAN = 5000  # cheap sanity ceiling; see filter_federation_index docstring


def _tag_filter_predicate(tag: str):
    """Match ``tag`` inside the JSON ``tags`` list column, dialect-agnostic.

    ``FederationHubSkill.tags`` is a JSON column (list[str]); neither SQLite
    (used by the test suite) nor a portable SQLAlchemy expression supports a
    native "JSON array contains" comparison across both backends used here
    (SQLite via the test suite, Postgres in prod) without dialect branching.
    Casting to TEXT and substring-matching a quoted token is the same
    dialect-agnostic trick this codebase already uses elsewhere for JSON
    membership-adjacent filters (e.g. Connector.slug.ilike search) — cheap,
    correct for the tag alphabet (skill tags are slug-like: no embedded
    quotes), and avoids a Postgres-only ``@>``/SQLite-only ``json_each``
    branch for what is, in this phase, a discovery filter rather than a
    security boundary.
    """
    needle = f'"{tag}"'
    return cast(FederationHubSkill.tags, String).ilike(f"%{needle}%")


def filter_federation_index(
    db: Session,
    *,
    source: str | None = None,
    license_id: str | None = None,
    trust_level: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[FederationHubSkill], int]:
    """Apply the filter and return (page, total_matching).

    Pure query-builder — no request/response shape here, so
    ``app.mcp.tools`` and any future surface can call it directly rather
    than round-tripping through HTTP. Every filter is AND-combined; omitted
    filters are no-ops (mirrors ``skill_routes.search_skills``'s optional-arg
    pattern).
    """
    query = db.query(FederationHubSkill)
    if source:
        query = query.filter(FederationHubSkill.upstream_source == source)
    if license_id:
        query = query.filter(FederationHubSkill.license == license_id)
    if trust_level:
        query = query.filter(FederationHubSkill.trust_level == trust_level)
    if tag:
        query = query.filter(_tag_filter_predicate(tag))
    if q:
        needle = f"%{q}%"
        query = query.filter(
            FederationHubSkill.title.ilike(needle) | FederationHubSkill.description.ilike(needle)
        )

    total = query.count()
    rows = query.order_by(FederationHubSkill.slug.asc()).offset(offset).limit(limit).all()
    return rows, total


def _row_to_bulk_shape(row: FederationHubSkill) -> dict[str, Any]:
    """Render one hub row in the exact shape ``BulkSkillItem`` accepts.

    ``federated_source`` here is ALWAYS ``"hermes-hub"`` — the bundle-skill
    identity namespace this repo records for hub-indexed federated content
    (see ``BundleSkill.federated_source`` / ``library_service.
    set_federated_like_in_bundle``), not the Hub snapshot's own
    ``upstream_source`` facet (clawhub/skills-sh/github/...), which is
    exposed separately as ``upstream_source`` for display/filter purposes.
    Conflating the two would silently break every existing federated
    bundle-membership row's identity contract.
    """
    return {
        "slug": row.slug,
        "title": row.title,
        "federated_source": "hermes-hub",
        "federated_slug": row.slug,
        "upstream_source": row.upstream_source,
        "trust_level": row.trust_level,
        "license": row.license,
        "tags": row.tags or [],
        "origin_url": row.origin_url,
        "install_path": row.install_path,
    }


@router.get("/api/federation/filter")
def get_federation_filter(
    source: str | None = Query(
        None, description="Filter by upstream source (clawhub, skills.sh, github, ...)"
    ),
    license: str | None = Query(
        None, description="Filter by recorded license (Q3: recorded, never enforced)"
    ),
    trust_level: str | None = Query(None, description="Filter by trust_level (community|trusted|builtin)"),
    tag: str | None = Query(None, description="Filter by a single tag membership"),
    q: str | None = Query(None, description="Free-text match on title/description"),
    limit: int = Query(50, ge=1, le=_MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Carve the ~154k-row federated index by source/license/trust_level/tag.

    Public, anonymous-safe read (same posture as ``GET /api/skills/external``
    and ``GET /api/bundles/discover`` — public discovery surfaces never
    require auth in this codebase). Writing the result into a bundle IS
    auth-gated, at the bulk-add endpoint, which is the correct boundary:
    browsing the index costs nothing; mutating a bundle requires ownership.

    Response ``results`` entries are the exact shape
    ``POST /api/bundles/{id}/skills/bulk``'s ``items`` array accepts
    (``federated_source`` + ``federated_slug``) — this is the "filter result
    is directly actionable" contract the phase requires. A client filters,
    then POSTs ``{"items": <these results, or a slice of them>}`` with zero
    reshaping.
    """
    rows, total = filter_federation_index(
        db,
        source=source,
        license_id=license,
        trust_level=trust_level,
        tag=tag,
        q=q,
        limit=limit,
        offset=offset,
    )
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {"source": source, "license": license, "trust_level": trust_level, "tag": tag, "q": q},
        "results": [_row_to_bulk_shape(r) for r in rows],
    }


@router.get("/api/federation/filter/facets")
def get_federation_filter_facets(db: Session = Depends(get_db)) -> dict[str, Any]:
    """The distinct source / trust_level values worth offering as filter
    chips — a UI needs this to render "carve by X" controls without
    hardcoding the source list (which already drifts: see
    ``app/services/federation.py:LIVE_SOURCES`` vs the Hub snapshot's own
    ``upstream_source`` facets, a DIFFERENT list — clawhub/skills.sh/github/
    official/lobehub/browse-sh/claude-marketplace, see hub_snapshot.py).

    License is deliberately OMITTED from facets: every row is NULL today
    (Q3 / bundles0811_p36_hub_license.py) — enumerating a facet with zero
    non-null values would render a dead control. Re-add once any row
    populates it.
    """
    sources = [row[0] for row in db.query(FederationHubSkill.upstream_source).distinct().all() if row[0]]
    trust_levels = [row[0] for row in db.query(FederationHubSkill.trust_level).distinct().all() if row[0]]
    return {"sources": sorted(sources), "trust_levels": sorted(trust_levels)}
