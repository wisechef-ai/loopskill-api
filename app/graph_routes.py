"""Graph extension HTTP surface (Phase B.5 + bundles0811 P6).

GET  /api/graph/related        — public read; queries any of the 7 edge types
GET  /api/graph/coverage       — public read; honest per-edge-type coverage
GET  /api/graph/neighborhood   — public read; lazy paginated, ADVISORY ONLY
POST /api/graph/replacements   — master-key-only; insert a manual replacement
GET  /api/graph/replacements   — public read; list all manual replacements

The router lives under `/api/graph/` so the middleware can grant blanket
public access via PUBLIC_PREFIXES. The POST endpoint validates the master
API key inline because the middleware exempted the prefix.

ADVISORY ONLY (control-plane lock, bundles0811 P6): every GET on this router
returns suggestions/coverage data. None of them install, apply, or mutate
anything a skill/bundle actually runs — that gate is unconditional and is
asserted by test.
"""

from __future__ import annotations

import base64
import hmac
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.graph_coverage import compute_coverage
from app.graph_extension import EDGE_TYPES, edges_for
from app.models import Skill, SkillReplacement

router = APIRouter(prefix="/api/graph", tags=["graph"])

NEIGHBORHOOD_DEFAULT_LIMIT = 20
NEIGHBORHOOD_MAX_LIMIT = 100


class GraphEdge(BaseModel):
    skill_slug: str
    edge_type: str
    weight: float
    evidence_count: int


class ReplacementIn(BaseModel):
    source_slug: str = Field(..., description="Slug being replaced")
    target_slug: str = Field(..., description="Slug doing the replacing")
    reason: str | None = Field(None, description="Curator note for audit log")


class ReplacementOut(BaseModel):
    source_slug: str
    target_slug: str
    reason: str | None
    created_by: str | None
    created_at: str


# ── Read: GET /api/graph/related ──────────────────────────────────────────


@router.get("/related", response_model=list[GraphEdge])
def graph_related(
    skill: str = Query(..., description="Source skill slug"),
    edge: str = Query(..., description=f"Edge type — one of {sorted(EDGE_TYPES)}"),
    min_weight: float = Query(0.0, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
):
    """Return edges of one type rooted at one skill.

    Public — no API key required (the prefix is in PUBLIC_PREFIXES). Accepts
    any of the seven edge types in `EDGE_TYPES`. Defensive about missing
    upstream data: returns [] (200) rather than 500 when a derivation
    table/column hasn't been provisioned yet.
    """
    if edge not in EDGE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown edge type {edge!r}; expected one of {sorted(EDGE_TYPES)}",
        )

    src = db.query(Skill).filter(Skill.slug == skill).first()
    if not src:
        raise HTTPException(status_code=404, detail=f"Skill '{skill}' not found")

    return edges_for(db, skill, edge, min_weight=min_weight)


# ── Read: GET /api/graph/coverage ──────────────────────────────────────────


class EdgeTypeCoverage(BaseModel):
    eligible_nodes: int
    covered_nodes: int
    coverage_pct: float
    last_built_at: str | None
    scope: str
    note: str
    deferred_federated_eligible_origins: int | None = None


class CoverageResponse(BaseModel):
    edge_types: dict[str, EdgeTypeCoverage]


@router.get("/coverage", response_model=CoverageResponse)
def graph_coverage(db: Session = Depends(get_db)):
    """Per-edge-type eligible/covered/coverage_pct/last_built_at — computed live.

    Every number here comes from a real query against the bound session (see
    `app.graph_coverage`); nothing is a placeholder. Reports honestly when a
    signal is local-only (tag_overlap, category_sibling, co_install,
    related_skills) versus spanning federated identities
    (bundle_co_membership) — never silently mixes the two.
    """
    return CoverageResponse(edge_types=compute_coverage(db))


# ── Read: GET /api/graph/neighborhood (lazy, paginated, ADVISORY ONLY) ─────


class NeighborhoodItem(BaseModel):
    skill_slug: str
    edge_type: str
    weight: float
    evidence_count: int


class NeighborhoodResponse(BaseModel):
    skill: str
    items: list[NeighborhoodItem]
    next_cursor: str | None
    advisory_only: bool = True
    note: str = (
        "Suggestions only. This endpoint never installs, applies, or mutates "
        "anything — the control-plane lock is unconditional."
    )


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
    # Rationale: a malformed/tampered cursor must not 500 the request — restart at page 0.
    except Exception:  # noqa: BLE001
        return 0


@router.get("/neighborhood", response_model=NeighborhoodResponse)
def graph_neighborhood(
    skill: str = Query(..., description="Source skill slug"),
    edge: str | None = Query(None, description=f"Optional edge type filter — one of {sorted(EDGE_TYPES)}"),
    limit: int = Query(NEIGHBORHOOD_DEFAULT_LIMIT, ge=1, le=NEIGHBORHOOD_MAX_LIMIT),
    cursor: str | None = Query(None, description="Opaque pagination cursor from a prior response"),
    min_weight: float = Query(0.0, ge=0.0, le=1.0),
    db: Session = Depends(get_db),
):
    """Lazy, paginated neighbor listing for one skill. ADVISORY ONLY.

    Lazy: computed on demand for the ONE requested skill (never materializes
    the whole graph — contrast with `GET /api/skills/graph`, which dumps
    everything). Paginated: `limit`/`cursor` slice the weight-sorted result;
    `next_cursor` is null once exhausted. Advisory only: this is a read
    surface for suggestions — no endpoint here installs or applies anything.
    """
    if edge is not None and edge not in EDGE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"unknown edge type {edge!r}; expected one of {sorted(EDGE_TYPES)}",
        )

    src = db.query(Skill).filter(Skill.slug == skill).first()
    if not src:
        raise HTTPException(status_code=404, detail=f"Skill '{skill}' not found")

    edge_types = [edge] if edge else sorted(EDGE_TYPES)
    merged: list[dict] = []
    for et in edge_types:
        merged.extend(edges_for(db, skill, et, min_weight=min_weight))
    merged.sort(key=lambda e: e["weight"], reverse=True)

    offset = _decode_cursor(cursor)
    page = merged[offset : offset + limit]
    next_offset = offset + limit
    next_cursor = _encode_cursor(next_offset) if next_offset < len(merged) else None

    return NeighborhoodResponse(
        skill=skill,
        items=[NeighborhoodItem(**item) for item in page],
        next_cursor=next_cursor,
    )


# ── Write: POST /api/graph/replacements (master-only) ─────────────────────


def _require_master(request: Request) -> None:
    """Inline master-key gate.

    /api/graph/* is in PUBLIC_PREFIXES so the middleware doesn't see writes.
    We check the static master key directly. Per-user keys are not allowed
    on this endpoint — replacement edges shape every consumer's graph and
    must come from a curator.
    """
    key = request.headers.get("x-api-key")
    if not key or not hmac.compare_digest(key, settings.API_KEY):
        raise HTTPException(status_code=401, detail="master API key required")


@router.post("/replacements", response_model=ReplacementOut, status_code=201)
def create_replacement(
    body: ReplacementIn,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a skill replacement record (master-only)."""
    _require_master(request)

    if body.source_slug == body.target_slug:
        raise HTTPException(status_code=422, detail="source and target must differ")

    src = db.query(Skill).filter(Skill.slug == body.source_slug).first()
    tgt = db.query(Skill).filter(Skill.slug == body.target_slug).first()
    if not src or not tgt:
        raise HTTPException(status_code=404, detail="unknown source_slug or target_slug")

    existing = (
        db.query(SkillReplacement)
        .filter(
            SkillReplacement.source_id == src.id,
            SkillReplacement.target_id == tgt.id,
        )
        .first()
    )
    if existing:
        return ReplacementOut(
            source_slug=src.slug,
            target_slug=tgt.slug,
            reason=existing.reason,
            created_by=existing.created_by,
            created_at=existing.created_at.isoformat() if existing.created_at else "",
        )

    repl = SkillReplacement(
        id=uuid4(),
        source_id=src.id,
        target_id=tgt.id,
        reason=body.reason,
        created_by="master",
    )
    db.add(repl)
    db.commit()
    db.refresh(repl)
    return ReplacementOut(
        source_slug=src.slug,
        target_slug=tgt.slug,
        reason=repl.reason,
        created_by=repl.created_by,
        created_at=repl.created_at.isoformat() if repl.created_at else "",
    )


@router.get("/replacements", response_model=list[ReplacementOut])
def list_replacements(db: Session = Depends(get_db)):
    """Public list of curator-confirmed replacements (audit transparency)."""
    rows = (
        db.query(SkillReplacement, Skill.slug.label("src_slug"))
        .join(Skill, Skill.id == SkillReplacement.source_id)
        .all()
    )
    out: list[ReplacementOut] = []
    # second join for target slug — keep it simple, two passes is fine on
    # this small a list
    for repl, src_slug in rows:
        tgt = db.query(Skill).filter(Skill.id == repl.target_id).first()
        if not tgt:
            continue
        out.append(
            ReplacementOut(
                source_slug=src_slug,
                target_slug=tgt.slug,
                reason=repl.reason,
                created_by=repl.created_by,
                created_at=repl.created_at.isoformat() if repl.created_at else "",
            )
        )
    return out
