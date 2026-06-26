"""Promotion + reconcile-report routes — portal_0610 B1 (§6.6).

Wires the previously-uncalled promotion engine into live request paths so the
``stable`` channel actually advances (it was a silent no-op — see
``services/promotion_sweep`` docstring).

Endpoints:
  POST /api/cookbooks/{cookbook_id}/reconcile-report
      A canary agent reports the OUTCOME of applying a skill version
      (success / reconcile_failed / rolled_back). Persists a ReconcileEvent and
      opportunistically promotes that version if its gate is now met.

  POST /api/admin/promotion-sweep
      Master-only. Batch-evaluates every canary-reported version and promotes
      those that pass. The scheduler/cron calls this; also callable by hand.

Auth mirrors reconcile_routes: ``auth_ctx`` on request.state, owner-or-master
for the per-cookbook report; master-only for the global sweep.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Bundle, BundleSkill, Skill
from app.services.promotion import OUTCOME_FAILED, OUTCOME_ROLLED_BACK, OUTCOME_SUCCESS, promote_if_eligible
from app.services.promotion_sweep import run_promotion_sweep

router = APIRouter(tags=["promotion"])

_VALID_OUTCOMES = {OUTCOME_SUCCESS, OUTCOME_FAILED, OUTCOME_ROLLED_BACK}


class ReconcileReportIn(BaseModel):
    """One canary apply-outcome report for a single skill version."""

    slug: str
    semver: str
    outcome: str  # success | reconcile_failed | rolled_back
    channel: str = "canary"
    failure_reason: str | None = None


@router.post("/api/cookbooks/{cookbook_id}/reconcile-report")
def reconcile_report(
    cookbook_id: str,
    body: ReconcileReportIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Any:
    """Record a canary reconcile outcome and opportunistically promote.

    The agent calls this AFTER applying a version, reporting whether it executed
    cleanly. A clean record feeds the promotion gate; once the gate passes, the
    version is promoted to ``stable`` here (fast path) without waiting for the
    scheduled sweep.

    Auth: owner of the cookbook, or master. Non-owner → 404 (no existence leak,
    parity with reconcile_routes §7).
    """
    auth_ctx: AuthContext = getattr(request.state, "auth_ctx", None) or AuthContext(scope="master")
    api_key_id = getattr(request.state, "api_key_id", None)

    if body.outcome not in _VALID_OUTCOMES:
        response.status_code = 422
        return {"error": "invalid_outcome", "valid": sorted(_VALID_OUTCOMES)}

    try:
        cb_uuid = UUID(cookbook_id)
    except (ValueError, AttributeError):
        response.status_code = 404
        return {"error": "cookbook_not_found"}

    cb = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    if cb is None:
        response.status_code = 404
        return {"error": "cookbook_not_found"}

    is_owner = auth_ctx.scope == "master" or (
        auth_ctx.user_id is not None and cb.bundle_owner == auth_ctx.user_id
    )
    if not is_owner:
        response.status_code = 404
        return {"error": "cookbook_not_found"}

    # Resolve the skill by slug AND confirm it's declared in this bundle
    # (a report can only concern a skill the bundle actually ships).
    skill = db.query(Skill).filter(Skill.slug == body.slug).first()
    if skill is None:
        response.status_code = 404
        return {"error": "skill_not_found"}
    cs = (
        db.query(BundleSkill)
        .filter(
            BundleSkill.bundle_id == cb.id,  # compat-alias
            BundleSkill.skill_id == skill.id,
            BundleSkill.source != "disabled",
        )
        .first()
    )
    if cs is None:
        response.status_code = 404
        return {"error": "skill_not_in_cookbook"}

    # Persist the canary outcome (the promotion engine reads these events).
    from app.services.promotion import record_reconcile_event

    record_reconcile_event(
        db,
        skill_id=skill.id,
        semver=body.semver,
        outcome=body.outcome,
        channel=body.channel,
        cookbook_id=cb.id,
        api_key_id=api_key_id if isinstance(api_key_id, UUID) else None,
        failure_reason=body.failure_reason,
    )

    # Fast-path promotion: if this report completed the gate, advance to stable now.
    gate = promote_if_eligible(db, skill.id, body.semver)
    promoted = gate.promotable and gate.reason != "already_promoted"

    return {
        "recorded": True,
        "skill": body.slug,
        "semver": body.semver,
        "outcome": body.outcome,
        "channel": body.channel,
        "promoted_to_stable": promoted,
        "gate_reason": gate.reason,
    }


@router.post("/api/admin/promotion-sweep")
def promotion_sweep(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Any:
    """Master-only batch promotion sweep across all canary-reported versions.

    The scheduler/cron calls this periodically so a version whose final
    qualifying canary success arrived between request-path promotions still
    advances to ``stable``. Idempotent.
    """
    auth_ctx: AuthContext = getattr(request.state, "auth_ctx", None) or AuthContext(scope="master")
    if auth_ctx.scope != "master":
        response.status_code = 403
        return {"error": "master_required"}

    result = run_promotion_sweep(db)
    return {"status": "ok", **result.to_dict()}
