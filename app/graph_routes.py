"""Graph extension HTTP surface (Phase B.5).

GET  /api/graph/related        — public read; queries any of the 7 edge types
POST /api/graph/replacements   — master-key-only; insert a manual replacement
GET  /api/graph/replacements   — public read; list all manual replacements

The router lives under `/api/graph/` so the middleware can grant blanket
public access via PUBLIC_PREFIXES. The POST endpoint validates the master
API key inline because the middleware exempted the prefix.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.graph_extension import EDGE_TYPES, edges_for
from app.models import Skill, SkillReplacement

router = APIRouter(prefix="/api/graph", tags=["graph"])


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


# ── Write: POST /api/graph/replacements (master-only) ─────────────────────


def _require_master(request: Request) -> None:
    """Inline master-key gate.

    /api/graph/* is in PUBLIC_PREFIXES so the middleware doesn't see writes.
    We check the static master key directly. Per-user keys are not allowed
    on this endpoint — replacement edges shape every consumer's graph and
    must come from a curator.
    """
    key = request.headers.get("x-api-key")
    if not key or key != settings.API_KEY:
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
