"""Agent-facing bundle convergence surface — mesh_0408 W5.

The second half of the moat loop. ``app/bundle_deployment_routes.py`` is the
CONTROL-PLANE surface (JWT/portal, Pro tier): it declares intent. This module
is the AGENT surface (api-key, lock #13 — the per-agent key IS the member
identity): the governed client agent resolves what its bundle should be
running, applies it on its own host, and reports what actually happened.

::

    POST /api/bundle-apply/{slug}/start        -> open a job, get the targets
    POST /api/bundle-apply/jobs/{job_id}/report -> report one skill's outcome
    GET  /api/bundle-apply/jobs/{job_id}        -> poll (agent-readable)

The split matters: the control plane never marks itself green. The status can
only reach ``converged`` because a member reported success AT the semver the
bundle currently resolves to (see ``app/services/bundle_apply.py``).

Mounted at ``/api/bundle-apply`` — a prefix distinct from ``/api/bundle-deploy``
so ``/jobs/{job_id}`` can never be shadowed by that router's
``/{cookbook_id}/...`` patterns.

Auth: a caller is entitled to a bundle when its fleet is SUBSCRIBED to that
bundle, or when the SHARED predicate ``authz.can_read_cookbook`` says so
(master / owner / org-read). A bundle-scoped key is confined to its one bundle
regardless. Anything else gets 404 — never 403 — so bundle existence does not
leak (``reconcile_routes`` §7 parity). See ``_entitled``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Bundle, BundleApplyJob, FleetMember, FleetSubscription
from app.services.bundle_apply import (
    ItemNotInJob,
    JobAlreadyTerminal,
    UnresolvableBundle,
    VALID_OUTCOMES,
    create_apply_job,
    job_items,
    job_to_dict,
    record_member_report,
)
from app.services.fleet_members import resolve_member_for_key

router = APIRouter(prefix="/api/bundle-apply", tags=["bundle-apply"])


class MemberReportIn(BaseModel):
    """One member's apply outcome for a single skill in the job."""

    slug: str
    semver: str
    outcome: str  # success | failed
    failure_reason: str | None = None


def _caller(request: Request) -> tuple[AuthContext, UUID | None, UUID | None]:
    """(auth_ctx, api_key_id, api_key_user_id) as the api-key middleware stamps them."""
    ctx: AuthContext = getattr(request.state, "auth_ctx", None) or AuthContext.anonymous()
    api_key_id = getattr(request.state, "api_key_id", None)
    user_id = getattr(request.state, "api_key_user_id", None)
    return (
        ctx,
        api_key_id if isinstance(api_key_id, UUID) else None,
        (user_id if isinstance(user_id, UUID) else None),
    )


def _entitled(
    db: Session,
    bundle: Bundle,
    ctx: AuthContext,
    member: FleetMember | None,
) -> bool:
    """May this caller converge (or report against) ``bundle``?

    Two arms, in a deliberate order:

    1. **Subscription** — the caller's fleet is ``FleetSubscription``-bound to
       this bundle. This is the primary predicate because it is the only one
       that expresses "this agent was actually *given* this bundle", and it is
       a real per-fleet grant rather than an identity coincidence.
    2. **Ownership** — delegated to ``authz.can_read_cookbook``, the SHARED
       predicate. Not re-implemented inline: it already handles master scope,
       owner-match, org read, and the bundle-scoped-key restriction, and W1 is
       making it tenant-aware. When that lands, this arm inherits the fix with
       no change here.

    The bundle-scoped-key guard runs FIRST and gates both arms: a key
    restricted to one bundle must not converge another one even if its fleet
    happens to be subscribed to that other bundle.

    W1 DEPENDENCY (also in /tmp/ISSUES-w5.md): ``can_read_cookbook``'s
    owner-match arm is presently a bare id comparison, and because one user can
    own two orgs, an owner-match passes across the tenant boundary by
    construction (trap V2). That is W1's to fix, in one place, for every caller.
    """
    if ctx.bundle_scope is not None and ctx.bundle_scope != bundle.id:
        return False
    if member is not None:
        sub = (
            db.query(FleetSubscription)
            .filter(
                FleetSubscription.fleet_id == member.fleet_id,
                FleetSubscription.bundle_id == bundle.id,
            )
            .first()
        )
        if sub is not None:
            return True
    return authz.can_read_cookbook(ctx, bundle)


@router.post("/{slug}/start")
def start_apply(
    slug: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Any:
    """Open a convergence job for the caller's bundle and return its targets.

    The returned ``targets`` are the bundle's CURRENT resolution — pin, else
    newest published version. Publish a patch and the next call returns the new
    semver: that is step 4 of the loop, and it is what the agent then has to
    actually install before it can report a matching semver back.
    """
    ctx, api_key_id, user_id = _caller(request)
    if ctx.scope == "anonymous" and api_key_id is None and user_id is None:
        response.status_code = 401
        return {"error": "authentication_required"}

    bundle = db.query(Bundle).filter(Bundle.slug == slug).first()
    if bundle is None:
        response.status_code = 404
        return {"error": "bundle_not_found"}

    member = resolve_member_for_key(db, api_key_id)
    if not _entitled(db, bundle, ctx, member):
        response.status_code = 404  # no existence leak (§7 parity)
        return {"error": "bundle_not_found"}

    try:
        job, targets = create_apply_job(
            db,
            bundle,
            member_id=member.id if member else None,
            requested_by_user_id=user_id,
        )
    except UnresolvableBundle as exc:
        response.status_code = 409
        return {
            "error": "no_resolvable_version",
            "unresolvable": exc.slugs,
            "detail": (
                "Every skill in this bundle would need a published version before "
                "convergence can be checked; opening an empty job would report a "
                "green that proves nothing."
            ),
        }

    return {
        "job_id": str(job.id),
        "bundle_id": str(bundle.id),
        "status": job.status,
        "terminal": False,
        "targets": [t.to_dict() for t in targets],
    }


def _load_job_for_caller(
    db: Session, job_id: str, request: Request
) -> tuple[BundleApplyJob | None, int, dict | None]:
    """Resolve a job the caller is entitled to. Returns (job, status_code, error)."""
    ctx, api_key_id, user_id = _caller(request)
    if ctx.scope == "anonymous" and api_key_id is None and user_id is None:
        return None, 401, {"error": "authentication_required"}
    try:
        jid = UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        return None, 404, {"error": "job_not_found"}

    job = db.query(BundleApplyJob).filter(BundleApplyJob.id == jid).first()
    if job is None:
        return None, 404, {"error": "job_not_found"}

    bundle = db.query(Bundle).filter(Bundle.id == job.bundle_id).first()
    if bundle is None:
        return None, 404, {"error": "job_not_found"}

    member = resolve_member_for_key(db, api_key_id)
    if not _entitled(db, bundle, ctx, member):
        return None, 404, {"error": "job_not_found"}
    return job, 200, None


@router.post("/jobs/{job_id}/report")
def report_apply_outcome(
    job_id: str,
    request: Request,
    response: Response,
    body: MemberReportIn = Body(...),
    db: Session = Depends(get_db),
) -> Any:
    """Record what the member ACTUALLY achieved for one skill in the job.

    ``success`` at a semver other than the job's expectation is recorded
    faithfully and leaves the job ``applying`` — an agent still sitting on the
    defective version cannot green the board by asserting it is fine.
    """
    job, code, err = _load_job_for_caller(db, job_id, request)
    if job is None:
        response.status_code = code
        return err

    if body.outcome not in VALID_OUTCOMES:
        response.status_code = 422
        return {"error": "invalid_outcome", "valid": sorted(VALID_OUTCOMES)}

    try:
        job, items = record_member_report(
            db,
            job,
            slug=body.slug,
            semver=body.semver,
            outcome=body.outcome,
            failure_reason=body.failure_reason,
        )
    except JobAlreadyTerminal as exc:
        response.status_code = 409
        return {"error": "job_already_terminal", "status": exc.status}
    except ItemNotInJob:
        response.status_code = 404
        return {"error": "skill_not_in_job", "slug": body.slug}

    return job_to_dict(job, items)


@router.get("/jobs/{job_id}")
def get_apply_job(
    job_id: str,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Any:
    """Poll a job's state — agent-readable counterpart of the portal endpoint."""
    job, code, err = _load_job_for_caller(db, job_id, request)
    if job is None:
        response.status_code = code
        return err
    return job_to_dict(job, job_items(db, job))
