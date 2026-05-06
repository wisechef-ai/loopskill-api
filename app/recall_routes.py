"""Hybrid recall endpoint — v7 Phase E.

POST /api/recall — given a natural-language query and the caller's tier,
returns the top-N skills ranked by a hybrid (vector + BM25) signal.

Auth: middleware validates the x-api-key header. Master key sees all tiers
(default), other callers default to free unless they pass tier_filter
explicitly. The endpoint is registered under /api in app/main.py.
"""

from __future__ import annotations

import json
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.embeddings import embed_text, is_model_loaded
from app.models import Cookbook, CookbookSkill, Skill, User
from app.ranking import TIER_RANK, combine, score_bm25, score_vector

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["recall"])


# ── Schemas ──────────────────────────────────────────────────────────────


class RecallIn(BaseModel):
    query: str
    local_context_summary: str = ""
    tier_filter: list[Literal["free", "cook", "operator"]] = Field(
        default_factory=lambda: ["free", "cook", "operator"]
    )
    limit: int = 10


class RecallHit(BaseModel):
    slug: str
    title: str
    score: float
    why_matched: str
    install_status: Literal["already_in_cookbook", "available", "tier_locked"]


class RecallOut(BaseModel):
    hits: list[RecallHit]
    used_fallback: bool
    backend: str  # "vector" | "bm25" | "hybrid"


# ── Service helpers ──────────────────────────────────────────────────────


def _decode_embedding(raw) -> list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return [float(x) for x in raw]
    if isinstance(raw, str):
        try:
            return [float(x) for x in json.loads(raw)]
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
    # pgvector returns objects exposing __iter__
    try:
        return [float(x) for x in raw]
    except Exception:
        return None


def _why(skill: Skill, query: str, vec: float, bm: float) -> str:
    bits: list[str] = []
    qt = {t for t in (query or "").lower().split() if t}
    title_tokens = {t for t in (skill.title or "").lower().split() if t}
    overlap = qt & title_tokens
    if overlap:
        bits.append(f"title: {','.join(sorted(overlap))[:60]}")
    related = skill.related_skills or []
    if isinstance(related, list) and related:
        rel_overlap = qt & {str(r).lower() for r in related}
        if rel_overlap:
            bits.append(f"tag-overlap: {','.join(sorted(rel_overlap))[:60]}")
    if vec >= 0.5:
        bits.append(f"vector:{vec:.2f}")
    if bm > 0.5:
        bits.append(f"bm25:{bm:.2f}")
    if skill.tier:
        bits.append(f"tier-match: {skill.tier}")
    return "; ".join(bits) or f"vector:{vec:.2f} bm25:{bm:.2f}"


def _allowed_tier_set(user_tier: Optional[str]) -> set[str]:
    """The set of skill tiers the caller's plan can install."""
    if user_tier is None:
        # Master / no-user (e.g. dev master key) — allow everything.
        return {"free", "cook", "operator"}
    rank = TIER_RANK.get(user_tier, -1)
    return {t for t, r in TIER_RANK.items() if r <= rank}


def recall_skills(
    db: Session,
    *,
    query: str,
    tier_filter: list[str] | None = None,
    limit: int = 10,
    user_id=None,
    is_master: bool = True,
    user_tier: Optional[str] = None,
) -> dict:
    """Service layer used by both the HTTP route and the MCP tool."""
    tier_filter = tier_filter or ["free", "cook", "operator"]
    limit = max(1, min(int(limit or 10), 50))

    candidates = (
        db.query(Skill)
        .filter(Skill.is_public == True)  # noqa: E712
        .filter(Skill.tier.in_(tier_filter))
        .all()
    )

    # Compute query embedding once.
    q_emb = embed_text(query or "")
    backend_used = "hybrid" if is_model_loaded() else "bm25"
    used_fallback = not is_model_loaded()

    # Cookbook membership (best-effort: ignore on schema mismatch).
    in_cookbook_skill_ids: set = set()
    if user_id is not None:
        try:
            rows = (
                db.query(CookbookSkill.skill_id)
                .join(Cookbook, Cookbook.id == CookbookSkill.cookbook_id)
                .filter(Cookbook.cookbook_owner == user_id)
                .filter(CookbookSkill.source != "disabled")
                .all()
            )
            in_cookbook_skill_ids = {r[0] for r in rows}
        except Exception as exc:  # noqa: BLE001
            logger.debug("cookbook lookup skipped: %s", exc)

    allowed_tiers = (
        {"free", "cook", "operator"} if is_master else _allowed_tier_set(user_tier)
    )

    scored: list[tuple[float, float, float, Skill]] = []
    for sk in candidates:
        emb = _decode_embedding(getattr(sk, "embedding", None))
        v = score_vector(q_emb, emb) if emb is not None else 0.0
        b = score_bm25(query, sk, db)
        if v == 0.0 and b == 0.0:
            continue
        tier_match = (sk.tier or "free") in allowed_tiers
        in_cb = sk.id in in_cookbook_skill_ids
        # We still include tier-locked items (with score=0 from combine) so the
        # caller can see them flagged as tier_locked; but rank by an unscaled
        # version so they don't crowd out matches.
        ranked = combine(v, b, tier_match=True, in_cookbook=in_cb)
        scored.append((ranked, v, b, sk))

    scored.sort(key=lambda row: row[0], reverse=True)
    top = scored[:limit]

    hits: list[RecallHit] = []
    for ranked, v, b, sk in top:
        if (sk.tier or "free") not in allowed_tiers:
            install_status = "tier_locked"
        elif sk.id in in_cookbook_skill_ids:
            install_status = "already_in_cookbook"
        else:
            install_status = "available"
        hits.append(
            RecallHit(
                slug=sk.slug,
                title=sk.title,
                score=round(float(ranked), 4),
                why_matched=_why(sk, query, v, b),
                install_status=install_status,
            )
        )

    return {
        "hits": [h.model_dump() for h in hits],
        "used_fallback": used_fallback,
        "backend": backend_used,
    }


# ── Route ────────────────────────────────────────────────────────────────


@router.post("/recall", response_model=RecallOut)
def post_recall(
    body: RecallIn,
    request: Request,
    db: Session = Depends(get_db),
) -> RecallOut:
    api_key_user_id = getattr(request.state, "api_key_user_id", None)
    is_master = api_key_user_id is None
    user_tier: Optional[str] = None
    if not is_master:
        user = db.query(User).filter(User.id == api_key_user_id).first()
        user_tier = user.subscription_tier if user else "free"

    result = recall_skills(
        db,
        query=body.query,
        tier_filter=body.tier_filter,
        limit=body.limit,
        user_id=api_key_user_id,
        is_master=is_master,
        user_tier=user_tier,
    )
    return RecallOut(**result)
