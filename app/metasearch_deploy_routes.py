"""Fleet-deploy of external skills — the north-star route (metasearch_0710 P3).

``POST /api/skills/metasearch/deploy`` — a fleet runner deploys a metasearch result
(skills.sh / github tap) to a fleet (bundle) in ONE motion. This is the phase that
connects federation to paying fleet operators (north_star_plan: build once → deploy
to every client agent → update once, everyone syncs). No federation competitor has
a model of a fleet runner's agents across machines; a search result that deploys to a
whole fleet is the moat they structurally cannot copy.

Flow (deploy-time-resolve invariant, §7.5):
  1. Auth: member owns the target bundle (reuses require_cookbook_tier +
     _resolve_owned_cookbook — the same guard as every other desired-state write).
  2. Pin: resolve the external SKILL.md ONCE and pin its content SHA
     (pin_external_for_deploy). ClawHub / non-redistributable / origin-outage →
     fail closed 4xx (not fleet-deployable). This is the ONLY upstream call.
  3. Desired-state: add/reactivate the BundleSkill row (FK-satisfied by the
     materialized skills.id) with pinned_version = the content SHA. Agents reconcile
     against THIS pinned row — they never re-resolve upstream on poll (§7.5).
  4. Funnel: emit metasearch.fleet_deploy — the north-star metric (search →
     external result → install intent → FLEET DEPLOY).

Whole-fleet target (Adam condition 3, 2026-07-10): v1 deploys to the fleet
(bundle), not per-selected-member — per-member targeting is next-sprint. ClawHub
is preview-only (Adam condition 2b) — its resolver returns no content so it fails
the pin and is not fleet-deployable, enforced by the resolver, not re-checked here.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.bundle_routes import (
    BUNDLE_SKILL_CAP,
    CookbookCtx,
    _resolve_owned_cookbook,
    _touch_bundle_generation,
    require_cookbook_tier,
)
from app.database import get_db
from app.models import BundleSkill, TelemetryEvent
from app.services.metasearch_deploy import pin_external_for_deploy

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["skills", "metasearch", "fleet-deploy"])


class FleetDeployIn(BaseModel):
    install_ref: str  # the metasearch card's ref: "{source}:{slug}"
    fleet_id: str  # target bundle/fleet id (whole-fleet deploy, condition 3)


def _decode_ref(install_ref: str) -> tuple[str, str] | None:
    if not install_ref or ":" not in install_ref:
        return None
    source, _, slug = install_ref.partition(":")
    source, slug = source.strip(), slug.strip()
    return (source, slug) if source and slug else None


def _record_deploy_event(
    db: Session, request: Request, *, source: str, slug: str, fleet_id: str, pin: str
) -> None:
    """The north-star funnel event: a metasearch result reached FLEET DEPLOY."""
    try:
        ev = TelemetryEvent(
            event_type="metasearch.fleet_deploy",
            skill_slug=slug or None,
            payload=json.dumps({"source": source, "slug": slug, "fleet_id": fleet_id, "pinned_sha": pin}),
            client_ip=(request.client.host if request.client else None),
        )
        db.add(ev)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("metasearch fleet_deploy event write failed", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.warning("metasearch fleet_deploy rollback also failed", exc_info=True)


@router.post("/metasearch/deploy", tags=["skills", "metasearch", "fleet-deploy"])
def metasearch_fleet_deploy(
    body: FleetDeployIn,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Deploy an external metasearch result to a fleet with a deploy-time content pin.

    404 if the ref is malformed or the skill can't be pinned (ClawHub/deep-link/
    origin outage — fail-closed, never a dead deploy). 403 on cap. The pinned
    BundleSkill row is what 30k agents reconcile against — never upstream (§7.5).
    """
    decoded = _decode_ref(body.install_ref)
    if decoded is None:
        raise HTTPException(status_code=422, detail="malformed_install_ref")
    source, slug = decoded

    # Curated skills use the normal add-to-bundle route; this endpoint is for
    # EXTERNAL fleet-deploy only (the federation north-star motion).
    if source == "recipes":
        raise HTTPException(status_code=422, detail="use_bundle_add_for_curated")

    # Ownership: member must own the target fleet (same guard as all desired-state
    # writes). _resolve_owned_cookbook raises 403/404 as appropriate.
    cb = _resolve_owned_cookbook(db, ctx, body.fleet_id)

    # Deploy-time resolve + pin (§7.5). ClawHub / non-redistributable / origin
    # outage → pinned=False → fail closed (not fleet-deployable).
    pin = pin_external_for_deploy(db, source, slug)
    if not pin.pinned:
        raise HTTPException(
            status_code=404,
            detail={
                "deployed": False,
                "reason": pin.reason,
                "hint": "source not fleet-deployable (preview-only or unresolvable)",
            },
        )

    # Cap check (Pro tier) — computed BEFORE mutation so a disabled-row
    # reactivation is also gated (council SHOULD 3: reactivating a disabled row
    # into an at-cap bundle would otherwise be the 26th active skill).
    def _at_cap_for_reactivation() -> bool:
        if ctx.tier not in ("pro", "cook"):  # cook=legacy alias, remove after 2026-06-10
            return False
        active = (
            db.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )
        return active >= BUNDLE_SKILL_CAP

    # Add/reactivate the desired-state row, pinned to the content-addressed semver
    # (reconcile selects the SkillVersion by this semver → serves OUR tarball).
    existing = (
        db.query(BundleSkill)
        .filter(BundleSkill.bundle_id == cb.id, BundleSkill.skill_id == pin.skill_uuid)
        .first()
    )
    if existing is not None:
        # A disabled row being reactivated counts as a NEW active skill → cap it.
        if existing.source == "disabled" and _at_cap_for_reactivation():
            raise HTTPException(
                status_code=403,
                detail={"deployed": False, "reason": "skill_cap_reached", "cap": BUNDLE_SKILL_CAP},
            )
        existing.source = "overridden"  # provenance: explicitly deploy-pinned
        existing.pinned_version = pin.pinned_semver
        _touch_bundle_generation(db, cb.id)
        db.commit()
        _record_deploy_event(
            db, request, source=source, slug=slug, fleet_id=str(cb.id), pin=pin.pinned_sha or ""
        )
        return {
            "deployed": True,
            "fleet_id": str(cb.id),
            "slug": pin.slug,
            "pinned_sha": pin.pinned_sha,
            "pinned_semver": pin.pinned_semver,
            "redeployed": True,
        }

    # New row: cap check (Pro tier), mirroring the bundle add route.
    if ctx.tier in ("pro", "cook"):  # cook=legacy alias, remove after 2026-06-10
        active = (
            db.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )
        if active >= BUNDLE_SKILL_CAP:
            raise HTTPException(
                status_code=403,
                detail={"deployed": False, "reason": "skill_cap_reached", "cap": BUNDLE_SKILL_CAP},
            )

    row = BundleSkill(
        bundle_id=cb.id,
        skill_id=pin.skill_uuid,
        source="overridden",  # deploy-pinned provenance
        pinned_version=pin.pinned_semver,
    )
    db.add(row)
    _touch_bundle_generation(db, cb.id)
    db.commit()
    _record_deploy_event(db, request, source=source, slug=slug, fleet_id=str(cb.id), pin=pin.pinned_sha or "")
    return {
        "deployed": True,
        "fleet_id": str(cb.id),
        "slug": pin.slug,
        "pinned_sha": pin.pinned_sha,
        "pinned_semver": pin.pinned_semver,
        "redeployed": False,
    }
