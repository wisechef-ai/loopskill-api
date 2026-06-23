"""Fleet sync service — aggregate, channel-aware sync across subscribed cookbooks.

Called by recipes_fleet_sync MCP tool. Iterates the fleet's FleetSubscription
rows and reconciles each cookbook to its subscription's CHANNEL target:

  canary → skills advance to the latest published semver
  stable → skills advance only to versions that passed the health/eval gate
           (SkillVersion.promoted_to_stable_at IS NOT NULL — Phase E)
  frozen → no version movement (the subscription holds its current pins)

evergreen_0206 Phase C made channels real (recon: they were inert labels —
sync_fleet echoed sub.channel but never filtered version selection by it).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.models import Cookbook, CookbookSkill, FleetSubscription, Skill
from app.services.channel_select import FROZEN, latest_version_for_channel


def _channel_outdated(db: Session, cookbook_id: UUID, channel: str) -> list[dict[str, Any]]:
    """Return skills in *cookbook_id* whose pin != the CHANNEL target version.

    canary → target = latest semver. stable → target = latest PROMOTED semver.
    A skill with no eligible target on its channel (e.g. stable but nothing
    promoted) is skipped — never downgraded, never advanced past the gate.
    """
    rows = (
        db.query(
            CookbookSkill.skill_id,
            Skill.slug,
            CookbookSkill.pinned_version,
        )
        .join(Skill, Skill.id == CookbookSkill.skill_id)
        .filter(
            CookbookSkill.bundle_id == cookbook_id,  # compat-alias
            CookbookSkill.source != "disabled",
        )
        .all()
    )

    changes: list[dict[str, Any]] = []
    for r in rows:
        target = latest_version_for_channel(db, r.skill_id, channel)
        if target is None:
            # No eligible version on this channel (stable w/ nothing promoted,
            # or frozen) — do not move this skill.
            continue
        if r.pinned_version != target:
            changes.append(
                {
                    "skill_id": r.skill_id,
                    "slug": r.slug,
                    "from": r.pinned_version,
                    "to": target,
                }
            )
    return changes


def sync_fleet(
    db: Session,
    fleet_id: UUID,
    *,
    dry_run: bool = False,
    ctx: AuthContext,
) -> list[dict[str, Any]]:
    """Iterate subscriptions for *fleet_id* and reconcile each per its channel.

    Returns a list of per-cookbook results, each shaped::

        {
            "cookbook_id": str,
            "channel": "canary" | "stable" | "frozen",
            "changes": [{slug, from, to, action}, ...],
            "applied": bool,
            "frozen": bool,   # True → channel=frozen, no movement
        }
    """
    subs = db.query(FleetSubscription).filter(FleetSubscription.fleet_id == fleet_id).all()

    results: list[dict[str, Any]] = []
    for sub in subs:
        cb_id = str(sub.bundle_id)
        channel = sub.channel

        # FROZEN: hold — no movement, report explicitly.
        if channel == FROZEN:
            results.append(
                {
                    "cookbook_id": cb_id,
                    "channel": channel,
                    "changes": [],
                    "applied": False,
                    "frozen": True,
                }
            )
            continue

        outdated = _channel_outdated(db, sub.bundle_id, channel)
        changes = [
            {"slug": o["slug"], "from": o["from"], "to": o["to"], "action": "update"} for o in outdated
        ]

        applied = False
        if not dry_run and outdated:
            for o in outdated:
                db.query(CookbookSkill).filter(
                    CookbookSkill.bundle_id == sub.bundle_id,  # compat-alias
                    CookbookSkill.skill_id == o["skill_id"],
                ).update({"pinned_version": o["to"]})
            # evergreen_0206 Phase A: advance the generation token on mutation.
            db.query(Cookbook).filter(Cookbook.id == sub.bundle_id).update(  # compat-alias
                {"updated_at": func.now()}, synchronize_session=False
            )
            db.commit()
            applied = True

        results.append(
            {
                "cookbook_id": cb_id,
                "channel": channel,
                "changes": changes,
                "applied": applied,
                "frozen": False,
            }
        )

    return results
