"""spotify_1507 Phase B — Bundle-lock + drift routes.

POST /api/bundles/{bundle_id}/lock
    Mint a new immutable bundle-lock revision (the "publish/freeze" action).
    Deploys install FROM the lock. Owner/master only.

GET  /api/bundles/{bundle_id}/lock
    Read the current (highest-revision) lock: revision, lock_hash, entries.
    Public for public bundles (a follower can see exactly what they'd get).

GET  /api/bundles/{bundle_id}/lock/history
    All lock revisions (audit / bump trail). Owner/master.

POST /api/bundles/{bundle_id}/drift
    Given a member's installed lockfile-state, return the THREE-WAY per-skill
    drift verdict (in-sync | behind | drift | missing) vs the current lock.
    This is the read-time drift computation that never clobbers a local edit.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.database import get_db
from app.models import Bundle
from app.services.drift_service import (
    classify_entry_drift,
    current_lock,
    mint_bundle_lock,
    prior_revision_hashes,
)

# Inner router carries the routes with NO surface prefix; the module-level
# `router` mounts it under BOTH /api/bundles and /api/cookbooks so the
# dual-surface symmetry contract (test_loopskill_bundle_surface_symmetry) holds
# — /api/cookbooks is the backward-compat alias of the primary /api/bundles.
_h = APIRouter(tags=["bundle-lock"])


def _ctx(request: Request) -> AuthContext | None:
    return getattr(request.state, "auth_ctx", None)


def _bundle_or_404(db: Session, bundle_id: str) -> Bundle:
    try:
        bid = UUID(bundle_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="bundle_not_found") from exc
    bundle = db.query(Bundle).filter(Bundle.id == bid).first()
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle_not_found")
    return bundle


def _require_owner_or_master(ctx: AuthContext | None, bundle: Bundle) -> None:
    """Authorize the owner/master-only lock verbs, WITHIN the caller's tenant.

    mesh_0408 W1 (P0), codex review of PR #202 finding 1. This helper used to
    grant on a bare ``ctx.user_id == bundle.bundle_owner``, with no tenant
    predicate anywhere in the module — the exact short-circuit the rest of this
    fix closes, on the routes that MINT and REPLAY a bundle's immutable lock.
    Because one account owns every client org it runs, that comparison was true
    across every tenant boundary: a key deployed at client B could freeze
    client A's bundle and read its full lock history (every skill slug, version
    and checksum, at every revision).

    Denial is 404 ``bundle_not_found``, never 403 ``not_bundle_owner`` (finding
    3): the module's own ``_bundle_or_404`` answers 404 for a bundle that does
    not exist, so a 403 here told an unauthorized caller that the id it guessed
    is real. Cross-tenant denial must be indistinguishable from not-found.
    """
    if ctx is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if ctx.scope == "master":
        return
    if ctx.scope == "user" and authz.owner_match_within_tenant(ctx, bundle):
        return
    raise HTTPException(status_code=404, detail="bundle_not_found")


class DriftRequestSkill(BaseModel):
    slug: str
    pinned_version: str | None = None
    checksum_sha256: str | None = None


class DriftRequest(BaseModel):
    installed: list[DriftRequestSkill]


@_h.post("/{bundle_id}/lock")
def mint_lock(bundle_id: str, request: Request, db: Session = Depends(get_db)):
    """Mint a new immutable bundle-lock revision. Owner/master only."""
    bundle = _bundle_or_404(db, bundle_id)
    _require_owner_or_master(_ctx(request), bundle)
    lock = mint_bundle_lock(db, bundle, created_by=getattr(_ctx(request), "user_id", None))
    return {
        "bundle_id": str(bundle.id),
        "revision": lock.revision,
        "lock_hash": lock.lock_hash,
        "entry_count": len(lock.locked_entries or []),
    }


@_h.get("/{bundle_id}/lock")
def get_lock(bundle_id: str, request: Request, db: Session = Depends(get_db)):
    """Read the current lock. Public for public bundles; else owner/master."""
    bundle = _bundle_or_404(db, bundle_id)
    if bundle.visibility != "public":
        _require_owner_or_master(_ctx(request), bundle)
    lock = current_lock(db, bundle.id)
    if lock is None:
        raise HTTPException(status_code=404, detail="no_lock_minted")
    return {
        "bundle_id": str(bundle.id),
        "revision": lock.revision,
        "lock_hash": lock.lock_hash,
        "locked_entries": lock.locked_entries,
        "created_at": lock.created_at.isoformat() if lock.created_at else None,
    }


@_h.get("/{bundle_id}/lock/history")
def lock_history(bundle_id: str, request: Request, db: Session = Depends(get_db)):
    """All lock revisions (audit trail). Owner/master."""
    from app.models import BundleLock

    bundle = _bundle_or_404(db, bundle_id)
    _require_owner_or_master(_ctx(request), bundle)
    locks = (
        db.query(BundleLock)
        .filter(BundleLock.bundle_id == bundle.id)
        .order_by(BundleLock.revision.desc())
        .all()
    )
    return {
        "bundle_id": str(bundle.id),
        "revisions": [
            {
                "revision": lk.revision,
                "lock_hash": lk.lock_hash,
                "entry_count": len(lk.locked_entries or []),
                "created_at": lk.created_at.isoformat() if lk.created_at else None,
            }
            for lk in locks
        ],
    }


@_h.post("/{bundle_id}/drift")
def compute_drift(bundle_id: str, body: DriftRequest, request: Request, db: Session = Depends(get_db)):
    """Three-way per-skill drift verdict vs the current lock.

    in-sync | behind (bundle moved, will converge) | drift (local edit,
    NOT clobbered) | missing (declared, not installed). extras (installed but
    not in the lock) are surfaced as harvest candidates.
    """
    bundle = _bundle_or_404(db, bundle_id)
    if bundle.visibility != "public":
        _require_owner_or_master(_ctx(request), bundle)
    lock = current_lock(db, bundle.id)
    if lock is None:
        raise HTTPException(status_code=404, detail="no_lock_minted")

    installed_by_slug = {s.slug: s for s in body.installed}
    locked_by_slug = {e.get("slug"): e for e in (lock.locked_entries or [])}

    results = []
    for slug, locked in locked_by_slug.items():
        inst = installed_by_slug.get(slug)
        inst_dict = (
            {"version": inst.pinned_version, "checksum_sha256": inst.checksum_sha256}
            if inst is not None
            else None
        )
        known = prior_revision_hashes(db, bundle.id, slug)
        verdict = classify_entry_drift(inst_dict, locked, known_hashes_by_rev=known)
        results.append(
            {
                "slug": slug,
                "verdict": verdict,
                "locked_version": locked.get("version"),
                "installed_version": inst.pinned_version if inst else None,
            }
        )

    extras = [
        {"slug": slug, "installed_version": s.pinned_version}
        for slug, s in installed_by_slug.items()
        if slug not in locked_by_slug
    ]

    return {
        "bundle_id": str(bundle.id),
        "lock_revision": lock.revision,
        "results": sorted(results, key=lambda r: r["slug"]),
        "extras": sorted(extras, key=lambda e: e["slug"]),
        "summary": {
            "in_sync": sum(1 for r in results if r["verdict"] == "in-sync"),
            "behind": sum(1 for r in results if r["verdict"] == "behind"),
            "drift": sum(1 for r in results if r["verdict"] == "drift"),
            "missing": sum(1 for r in results if r["verdict"] == "missing"),
            "extras": len(extras),
        },
    }


# Dual-surface mount: /api/bundles (primary) + /api/cookbooks (compat alias).
router = APIRouter()
router.include_router(_h, prefix="/api/bundles", tags=["bundle-lock"])
router.include_router(_h, prefix="/api/cookbooks", tags=["cookbooks"])  # compat-alias
