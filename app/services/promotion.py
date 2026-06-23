"""Health/eval-gated channel promotion — evergreen_0206 Phase E.

THE "EXECUTES WITHOUT ERRORS" MECHANISM (decisions #14). A skill version enters
a cookbook on the CANARY channel. It advances canary→stable ONLY after passing a
health/eval gate observed on canary-channel agents. This is "agents that execute
without errors" as a *mechanism*, not a hope: Varys-on-canary catches a bad
version before it reaches Chef-on-stable.

The gate (per version):
  * DEFAULT gate (no eval.yaml declared): "no canary agent reported
    reconcile_failed or rollback for this version within the observation window,
    AND at least `min_success` successful canary reconciles were observed."
  * DECLARED gate (eval.yaml): a cookbook/skill may tighten it — require N
    distinct canary agents, a longer window, or a minimum success count.

frozen never advances. A version that has ANY canary failure in-window is BLOCKED
from promotion (the bad version never reaches stable).

The engine writes SkillVersion.promoted_to_stable_at (Phase C reads it for
stable-channel version selection). Idempotent: re-running never demotes and never
double-promotes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import ReconcileEvent, SkillVersion

# Default gate parameters (used when no eval.yaml is declared).
DEFAULT_OBSERVATION_HOURS = 24
DEFAULT_MIN_SUCCESS = 1

# Outcome vocabulary (mirrors ReconcileEvent.outcome).
OUTCOME_SUCCESS = "success"
OUTCOME_FAILED = "reconcile_failed"
OUTCOME_ROLLED_BACK = "rolled_back"
_FAILURE_OUTCOMES = {OUTCOME_FAILED, OUTCOME_ROLLED_BACK}


@dataclass(frozen=True)
class GateConfig:
    """Promotion gate parameters. Defaults = the loose-but-safe default gate."""

    observation_hours: int = DEFAULT_OBSERVATION_HOURS
    min_success: int = DEFAULT_MIN_SUCCESS
    min_distinct_agents: int = 1

    @classmethod
    def from_eval_yaml(cls, data: dict | None) -> GateConfig:
        """Build a GateConfig from a parsed eval.yaml dict (or defaults)."""
        if not data:
            return cls()
        gate = data.get("promotion_gate", data)
        return cls(
            observation_hours=int(gate.get("observation_hours", DEFAULT_OBSERVATION_HOURS)),
            min_success=int(gate.get("min_success", DEFAULT_MIN_SUCCESS)),
            min_distinct_agents=int(gate.get("min_distinct_agents", 1)),
        )


@dataclass
class GateResult:
    promotable: bool
    reason: str
    success_count: int = 0
    failure_count: int = 0
    distinct_agents: int = 0


def evaluate_gate(
    db: Session,
    skill_id: UUID,
    semver: str,
    *,
    config: GateConfig | None = None,
    now: datetime | None = None,
) -> GateResult:
    """Evaluate the promotion gate for a (skill, version) from canary telemetry.

    Promotable iff, within the observation window on the CANARY channel:
      - zero failure/rollback events, AND
      - >= min_success successful reconciles, AND
      - >= min_distinct_agents distinct reporting agents.
    """
    config = config or GateConfig()
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(hours=config.observation_hours)

    events = (
        db.query(ReconcileEvent)
        .filter(
            ReconcileEvent.skill_id == skill_id,
            ReconcileEvent.semver == semver,
            ReconcileEvent.channel == "canary",
            ReconcileEvent.created_at >= window_start,
        )
        .all()
    )

    successes = [e for e in events if e.outcome == OUTCOME_SUCCESS]
    failures = [e for e in events if e.outcome in _FAILURE_OUTCOMES]
    distinct_agents = len({e.api_key_id for e in successes if e.api_key_id is not None})
    # Anonymous self-test successes (api_key_id NULL) still count toward min_success
    # but not toward distinct-agent requirements.

    if failures:
        return GateResult(
            promotable=False,
            reason=f"blocked: {len(failures)} canary failure(s) in window",
            success_count=len(successes),
            failure_count=len(failures),
            distinct_agents=distinct_agents,
        )
    if len(successes) < config.min_success:
        return GateResult(
            promotable=False,
            reason=(f"insufficient canary successes " f"({len(successes)}/{config.min_success})"),
            success_count=len(successes),
            failure_count=0,
            distinct_agents=distinct_agents,
        )
    if config.min_distinct_agents > 1 and distinct_agents < config.min_distinct_agents:
        return GateResult(
            promotable=False,
            reason=(
                f"insufficient distinct canary agents " f"({distinct_agents}/{config.min_distinct_agents})"
            ),
            success_count=len(successes),
            failure_count=0,
            distinct_agents=distinct_agents,
        )

    return GateResult(
        promotable=True,
        reason="gate passed",
        success_count=len(successes),
        failure_count=0,
        distinct_agents=distinct_agents,
    )


def promote_if_eligible(
    db: Session,
    skill_id: UUID,
    semver: str,
    *,
    config: GateConfig | None = None,
    now: datetime | None = None,
) -> GateResult:
    """Evaluate the gate and, if passed, flip promoted_to_stable_at.

    Idempotent: a version already promoted is left as-is (never re-stamped,
    never demoted). Returns the GateResult.
    """
    now = now or datetime.now(timezone.utc)
    version = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id, SkillVersion.semver == semver)
        .first()
    )
    if version is None:
        return GateResult(promotable=False, reason="version_not_found")

    # Already promoted → idempotent no-op.
    if version.promoted_to_stable_at is not None:
        return GateResult(promotable=True, reason="already_promoted")

    result = evaluate_gate(db, skill_id, semver, config=config, now=now)
    if result.promotable:
        version.promoted_to_stable_at = now
        db.commit()
    return result


def record_reconcile_event(
    db: Session,
    *,
    skill_id: UUID,
    semver: str,
    outcome: str,
    channel: str = "canary",
    cookbook_id: UUID | None = None,
    api_key_id: UUID | None = None,
    failure_reason: str | None = None,
) -> ReconcileEvent:
    """Persist one canary reconcile outcome (called by the Phase D client path)."""
    ev = ReconcileEvent(
        bundle_id=cookbook_id,  # compat-alias
        skill_id=skill_id,
        semver=semver,
        channel=channel,
        outcome=outcome,
        failure_reason=failure_reason,
        api_key_id=api_key_id,
    )
    db.add(ev)
    db.commit()
    return ev
