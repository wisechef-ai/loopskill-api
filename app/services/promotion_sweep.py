"""Promotion sweep — portal_0610 B1 (§6.6).

THE FIX for the single biggest find in the stress sweep: the promotion engine
(`promotion.promote_if_eligible` / `evaluate_gate`) was complete and correct but
had ZERO non-test callers. Nothing ever wrote `SkillVersion.promoted_to_stable_at`,
so `channel_select(stable)` always returned None and every `stable` fleet/cookbook
silently skipped every skill forever — `stable` was a dead synonym for `frozen`.

This module is the missing caller. `run_promotion_sweep` walks every
(skill_id, semver) pair that has CANARY reconcile telemetry and not-yet-promoted
versions, and calls `promote_if_eligible` on each. A version that has passed its
gate (enough clean canary successes, no failures in-window) gets
`promoted_to_stable_at` stamped — and only then does the `stable` channel advance
to it.

Two entry points use this:
  * The reconcile-report intake route opportunistically promotes the single
    version an agent just reported on (fast path — promotes as soon as the gate
    is met, no waiting for a sweep tick).
  * A scheduled admin sweep (`POST /api/admin/promotion-sweep`) batch-evaluates
    everything with canary telemetry — the catch-all so a version whose final
    qualifying success arrived without a same-request promotion still advances.

Idempotent and safe to run repeatedly: `promote_if_eligible` never demotes and
never re-stamps an already-promoted version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import ReconcileEvent, SkillVersion
from app.services.promotion import GateConfig, promote_if_eligible


@dataclass
class SweepResult:
    """Summary of one promotion-sweep pass."""

    evaluated: int = 0
    promoted: int = 0
    promoted_pairs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "evaluated": self.evaluated,
            "promoted": self.promoted,
            "promoted_pairs": self.promoted_pairs,
        }


def _canary_candidate_pairs(db: Session) -> list[tuple[UUID, str]]:
    """Distinct (skill_id, semver) pairs that have CANARY telemetry AND a
    not-yet-promoted SkillVersion row.

    A pair already promoted (promoted_to_stable_at NOT NULL) is excluded — the
    gate would short-circuit to ``already_promoted`` anyway, but filtering here
    keeps the sweep cheap.
    """
    # All canary-reported (skill, semver) pairs.
    reported = (
        db.query(ReconcileEvent.skill_id, ReconcileEvent.semver)
        .filter(ReconcileEvent.channel == "canary")
        .distinct()
        .all()
    )
    if not reported:
        return []

    # Keep only pairs whose SkillVersion exists and is not yet promoted.
    out: list[tuple[UUID, str]] = []
    for skill_id, semver in reported:
        v = (
            db.query(SkillVersion)
            .filter(
                SkillVersion.skill_id == skill_id,
                SkillVersion.semver == semver,
                SkillVersion.promoted_to_stable_at.is_(None),
            )
            .first()
        )
        if v is not None:
            out.append((skill_id, semver))
    return out


def run_promotion_sweep(
    db: Session,
    *,
    config: GateConfig | None = None,
    now: datetime | None = None,
) -> SweepResult:
    """Evaluate the promotion gate for every canary-reported, unpromoted version
    and stamp ``promoted_to_stable_at`` on those that pass.

    Returns a SweepResult. Idempotent.
    """
    result = SweepResult()
    for skill_id, semver in _canary_candidate_pairs(db):
        result.evaluated += 1
        gate = promote_if_eligible(db, skill_id, semver, config=config, now=now)
        if gate.promotable and gate.reason != "already_promoted":
            result.promoted += 1
            result.promoted_pairs.append({"skill_id": str(skill_id), "semver": semver})
    return result
