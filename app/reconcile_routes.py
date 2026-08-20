"""Reconcile HTTP endpoint — evergreen_0206 Phase D.

GET /api/cookbooks/{cookbook_id}/reconcile

The thin client polls this with If-None-Match: <generation>. The endpoint:
  1. Resolves ownership FIRST (tenant isolation — reconcile-contract §7). A
     non-owner gets 404, never 304/200, so cookbook existence never leaks.
  2. Enforces the per-agent abuse ceiling (Phase A — 60/5min per api_key_id).
  3. CHEAP 304: if the caller's If-None-Match == the cookbook's current
     generation (Bundle.updated_at), returns 304 after ONE indexed lookup —
     the reconcile engine is never invoked. ~99% of polls collapse to this.
  4. On 200: runs the reconcile engine (Phase B) against the caller's reported
     local lockfile state and returns the {add,update,remove,drift} diff plus
     the new generation (for the client's next If-None-Match).

POST body carries the caller's local lockfile state so the server can compute
add/update/remove/drift. A bare GET (no body) treats local as empty.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.database import get_db
from app import authz
from app.models import Bundle
from app.reconcile_abuse_ceiling import check_reconcile_abuse_ceiling
from app.services.reconcile import recipes_reconcile

_h = APIRouter(tags=["reconcile"])  # handlers registered prefix-free; dual-mounted below


class LocalSkillIn(BaseModel):
    slug: str
    pinned_version: str | None = None
    sha256: str | None = None


class ReconcileIn(BaseModel):
    local: list[LocalSkillIn] = []
    local_connectors: list[dict[str, Any]] | None = None  # Phase B — additive, backward-compatible
    prune: bool = False
    dry_run: bool = True  # the poll path defaults to plan (read-only)


def _generation_token(cb: Bundle) -> str:
    """Stable string form of Bundle.updated_at for ETag / If-None-Match."""
    return cb.updated_at.isoformat() if cb.updated_at else ""


@_h.post("/{cookbook_id}/reconcile")
def reconcile_cookbook(
    cookbook_id: str,
    body: ReconcileIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> Any:
    """Conditional reconcile poll (see module docstring)."""
    auth_ctx: AuthContext = getattr(request.state, "auth_ctx", None) or AuthContext(scope="master")
    api_key_id = getattr(request.state, "api_key_id", None)

    # ── Per-agent abuse ceiling (Phase A) — generous, same all tiers. ────
    if api_key_id is not None:
        ceiling = check_reconcile_abuse_ceiling(str(api_key_id))
        if not ceiling.allowed:
            response.status_code = 429
            response.headers["Retry-After"] = str(ceiling.retry_after)
            return {"error": "rate_limited", "retry_after": ceiling.retry_after}

    # ── Resolve bundle + OWNERSHIP FIRST (tenant isolation §7). ────────
    try:
        cb_uuid = UUID(cookbook_id)
    except (ValueError, AttributeError):
        response.status_code = 404
        return {"error": "bundle_not_found"}

    cb = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    if cb is None:
        response.status_code = 404
        return {"error": "bundle_not_found"}

    # Deployment read access: master, owner, or an active follower of a public
    # bundle. Follows intentionally grant reconcile only; all bundle mutation
    # paths remain owner-only through their existing write predicates.
    # mesh_0408 W1 (P0): tenant-scoped owner-match. The bare owner-match this
    # replaces handed one client's full reconcile diff (every skill, version
    # and connector) to a member key deployed at a DIFFERENT client, because
    # both fleets are run by the same account.
    is_owner = authz.owner_match_within_tenant(auth_ctx, cb)
    is_follower = not is_owner and authz.can_reconcile_cookbook(auth_ctx, cb, db=db)
    if not is_owner and not is_follower:
        response.status_code = 404
        return {"error": "bundle_not_found"}
    if is_follower and not body.dry_run:
        response.status_code = 403
        return {"error": "read_only_follow"}

    generation = _generation_token(cb)

    # ── CHEAP 304: If-None-Match matches generation → no diff computed. ──
    inm = request.headers.get("if-none-match", "").strip().strip('"')
    if inm and inm == generation:
        response.status_code = 304
        response.headers["ETag"] = f'"{generation}"'
        return Response(status_code=304, headers={"ETag": f'"{generation}"'})

    # ── 200: compute the reconcile diff via the Phase B engine. ─────────
    local = [{"slug": s.slug, "pinned_version": s.pinned_version, "sha256": s.sha256} for s in body.local]
    result = recipes_reconcile(
        db,
        cookbook_id=cookbook_id,
        local=local,
        local_connectors=body.local_connectors,
        prune=body.prune,
        dry_run=body.dry_run,
        ctx=auth_ctx,
    )
    response.headers["ETag"] = f'"{generation}"'
    return result


# Dual-mount: the bundle surface is primary; /api/cookbooks kept as the
# backward-compat alias (same handlers, both prefixes).  # compat-alias
router = APIRouter()
router.include_router(_h, prefix="/api/bundles")
router.include_router(_h, prefix="/api/cookbooks")  # compat-alias
