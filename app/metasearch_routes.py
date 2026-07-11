"""Unified metasearch route (metasearch_0710 P0).

GET /api/skills/metasearch — ONE ranked list across curated + skills.sh + ClawHub
+ github taps + the other live sources. This is the "intact seam": no
second-class 'external' namespace, no toggle, no stored catalog count (Spotify
model, Adam Q2). Curated interleaves with external by rank; curated wins ties and
carries the quality chip.

North-star framing: this surface is a FEEDER to the fleet-deploy motion. Every
card that is `deployable` will (P3) carry the "Deploy to fleet" action; ClawHub
is searchable + ad-hoc-installable but NOT deployable in v1 (Adam condition 2b).

Funnel instrumentation (plan §1.5.4 — non-optional): every metasearch emits a
``metasearch.query`` telemetry event, and the payload records how many external
results were surfaced. This is what connects the surface to the one number
(external-search → fleet-deploy → cap-hit). Instrumentation ships WITH the
surface in P0, not after.

This route is ADDITIVE — it does not touch the existing /api/skills/search
(internal keyword) or /api/skills/external (legacy namespaced) routes. The crawl
delete + those routes' retirement is a later phase; P0 stands up the new surface
alongside them so nothing in flight breaks.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, joinedload

from app._skill_helpers import _install_counts_for, _skill_to_out
from app.database import get_db
from app.models import Skill, TelemetryEvent
from app.services.metasearch import merge_unified, unify_curated, unify_external
from app.services.metasearch_fanout import DEFAULT_FANOUT_SOURCES, fan_out
from app.services.metasearch_install import resolve_install

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["skills", "metasearch"])

_CURATED_CAP = 50  # curated candidates pulled before the merge caps the page


def _curated_candidates(db: Session, q: str | None, limit: int) -> list[dict]:
    """Pull curated (internal, public) skill rows matching the query, as
    _skill_to_out dicts. Mirrors the literal-match pass of /api/skills/search but
    only the public catalog (the federation wall: never surface private skills)."""
    query = (
        db.query(Skill)
        .options(joinedload(Skill.versions), joinedload(Skill.creator))
        .filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
    )
    if q:
        like = f"%{q}%"
        query = query.filter(
            Skill.title.ilike(like)
            | Skill.description.ilike(like)
            | Skill.category.ilike(like)
            | Skill.readme.ilike(like)
        )
    rows = query.limit(limit).all()
    if not rows:
        return []
    counts = _install_counts_for(db, [s.id for s in rows])
    out = []
    for s in rows:
        skill_out = _skill_to_out(s, *counts.get(s.id, (0, 0)))
        d = skill_out.model_dump() if hasattr(skill_out, "model_dump") else dict(skill_out)
        # unify_curated reads install_count + slug/title/description/updated_at
        d["install_count"] = d.get("install_count_total", 0)
        out.append(d)
    return out


def _record_funnel_event(db: Session, request: Request, *, q: str | None, result: dict) -> None:
    """Emit the metasearch.query funnel event (plan §1.5.4). Best-effort — a
    telemetry write must never break the search response."""
    try:
        external_count = sum(1 for s in result["skills"] if s["source"] != "recipes")
        deployable_count = sum(1 for s in result["skills"] if s["deployable"])
        payload = {
            "q": (q or "")[:120],
            "result_count": result["result_count"],
            "external_count": external_count,
            "deployable_count": deployable_count,
            "sources_ok": result["sources_ok"],
            "sources_degraded": result["sources_degraded"],
        }
        ev = TelemetryEvent(
            event_type="metasearch.query",
            skill_slug=None,
            payload=json.dumps(payload),
            client_ip=(request.client.host if request.client else None),
        )
        db.add(ev)
        db.commit()
    # Rationale: telemetry is fire-and-forget; never 500 the search on a log write.
    except Exception:  # noqa: BLE001
        logger.warning("metasearch funnel event write failed", exc_info=True)
        # Council finding 5: rollback itself can raise on a broken/disconnected
        # session — guard it independently so a telemetry failure never surfaces
        # as a search failure.
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("metasearch funnel rollback also failed", exc_info=True)


@router.get("/metasearch", tags=["skills", "metasearch"])
def metasearch(
    request: Request,
    q: str | None = Query(
        None, description="Search query. Empty returns curated + empty-query-capable sources."
    ),
    page_size: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """Unified metasearch: one ranked list across curated + live external sources.

    Response shape (the intact seam — NO internal/external split, NO stored count):
      {skills: [UnifiedSkill...], result_count, sources_ok, sources_degraded, source_count}
    """
    curated_rows = _curated_candidates(db, q, _CURATED_CAP)
    curated = [unify_curated(r) for r in curated_rows]

    # Concurrent, rate-limited, deadline-bounded fan-out (council condition 1).
    fanout = fan_out(q or "", sources=DEFAULT_FANOUT_SOURCES)
    external = [unify_external(skill, raw_row=raw) for skill, raw in fanout.pairs]

    result = merge_unified(
        curated,
        external,
        sources_ok=["recipes", *fanout.sources_ok],
        sources_degraded=fanout.sources_degraded,
    )
    payload = result.to_dict()
    payload["skills"] = payload["skills"][:page_size]
    payload["result_count"] = len(payload["skills"])

    _record_funnel_event(db, request, q=q, result=result.to_dict())
    return payload


def _install_command_matrix(source: str, origin_url: str | None, preview_only: bool) -> dict:
    """Per-agent templated install commands (§6 / P3b fallback path) for the
    ad-hoc / single-agent visitor. The LoopSkill fleet-deploy motion (P3) is the
    real action for operators; this is the copy-paste teaser for everyone else.
    ClawHub (preview_only) shows an install-from-origin line, never a rehost."""
    origin = origin_url or ""
    if preview_only:
        # ClawHub: user installs from ClawHub's own origin (we never rehost).
        return {
            "clawhub": f"clawhub install {origin.rsplit('/', 1)[-1]}" if origin else "",
            "note": "Community source — install from origin; not fleet-deployable in v1.",
        }
    return {
        "hermes": f"hermes skills add {origin}" if origin else "",
        "claude_code": "/plugin marketplace add <repo> → /plugin install (when repo is a marketplace)",
        "generic": f"Fetch SKILL.md from {origin}" if origin else "",
    }


@router.get("/metasearch/install", tags=["skills", "metasearch"])
def metasearch_install(
    request: Request,
    install_ref: str = Query(..., description="The install_ref from a metasearch card ('{source}:{slug}')."),
    db: Session = Depends(get_db),
):
    """Resolve ONE metasearch card to an installable/previewable SKILL.md body.

    Fail-closed (plan §5.3): an unresolvable ref returns 404 with a reason, so the
    UI never shows a dead install button — the card should not have been actionable.

    Response: {resolved, source, slug, body, origin_url, preview_only, commands}.
    ``preview_only`` (ClawHub) means the body is from the source's own API and the
    card is NOT fleet-deployable in v1 (decision #6). Fetch-origin sources return a
    real installable body.
    """
    resolved = resolve_install(install_ref)
    _record_install_intent_event(db, request, resolved=resolved)
    if not resolved.resolved:
        raise HTTPException(status_code=404, detail={"resolved": False, "reason": resolved.reason})
    out = resolved.to_dict()
    out["commands"] = _install_command_matrix(resolved.source, resolved.origin_url, resolved.preview_only)
    return out


def _record_install_intent_event(db: Session, request: Request, *, resolved) -> None:
    """Funnel event: search → external result → INSTALL INTENT (§1.5.4). This is
    the step between search and fleet-deploy the north-star funnel measures."""
    try:
        payload = {
            "source": resolved.source,
            "slug": resolved.slug,
            "resolved": resolved.resolved,
            "preview_only": resolved.preview_only,
            "reason": resolved.reason,
        }
        ev = TelemetryEvent(
            event_type="metasearch.install_intent",
            skill_slug=resolved.slug or None,
            payload=json.dumps(payload),
            client_ip=(request.client.host if request.client else None),
        )
        db.add(ev)
        db.commit()
    # Rationale: telemetry is fire-and-forget; never break the install response.
    except Exception:  # noqa: BLE001
        logger.warning("metasearch install_intent event write failed", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("metasearch install_intent rollback also failed", exc_info=True)
