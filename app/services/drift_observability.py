"""Drift observability surface — evergreen_0206 Phase I.

NO NEW WRITE SURFACE. This phase READS the telemetry the other phases already
emit (reconcile_events from Phase D/E, FleetPing liveness) and produces a
per-cookbook / per-fleet drift+health view.

Surfaces:
  - reconcile health per cookbook: last success, last rollback, failure count,
    which skill-versions are currently failing on canary
  - liveness: how many distinct agents pinged recently (from FleetPing)

The reasoning is read-only; mirrors Hermes update_stale_dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import FleetPing, ReconcileEvent

DEFAULT_STALE_DAYS = 7


@dataclass
class CookbookDriftStatus:
    cookbook_id: str
    last_reconcile_at: str | None = None
    last_rollback_at: str | None = None
    success_count: int = 0
    failure_count: int = 0
    # Skill versions currently showing failures (canary), worth attention.
    failing_versions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookbook_id": self.cookbook_id,
            "last_reconcile_at": self.last_reconcile_at,
            "last_rollback_at": self.last_rollback_at,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "failing_versions": self.failing_versions,
            "healthy": self.failure_count == 0,
        }


def cookbook_drift_status(
    db: Session,
    cookbook_id: UUID,
    *,
    window_days: int = DEFAULT_STALE_DAYS,
    now: datetime | None = None,
) -> CookbookDriftStatus:
    """Read reconcile telemetry for one cookbook and summarize drift/health.

    Reads reconcile_events only — no writes. Caller is responsible for having
    already authorized access to this cookbook (tenant isolation, §7).
    """
    now = now or datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)

    events = (
        db.query(ReconcileEvent)
        .filter(
            ReconcileEvent.cookbook_id == cookbook_id,
            ReconcileEvent.created_at >= window_start,
        )
        .all()
    )

    status = CookbookDriftStatus(cookbook_id=str(cookbook_id))
    failing: dict[tuple, dict[str, Any]] = {}
    last_success: datetime | None = None
    last_rollback: datetime | None = None

    for e in events:
        if e.outcome == "success":
            status.success_count += 1
            if last_success is None or e.created_at > last_success:
                last_success = e.created_at
        else:  # reconcile_failed | rolled_back
            status.failure_count += 1
            if e.outcome == "rolled_back" and (last_rollback is None or e.created_at > last_rollback):
                last_rollback = e.created_at
            key = (str(e.skill_id), e.semver)
            failing.setdefault(
                key,
                {"skill_id": str(e.skill_id), "semver": e.semver, "count": 0, "outcome": e.outcome},
            )
            failing[key]["count"] += 1

    status.last_reconcile_at = last_success.isoformat() if last_success else None
    status.last_rollback_at = last_rollback.isoformat() if last_rollback else None
    status.failing_versions = list(failing.values())
    return status


def fleet_liveness(
    db: Session, *, window_days: int = DEFAULT_STALE_DAYS, now: datetime | None = None
) -> dict[str, Any]:
    """Count distinct agents that pinged within the window (from FleetPing).

    FleetPing is mathematically anonymous (keyed blake2b hash + day). We can only
    count distinct (salt_hash) seen recently — never identify an agent. That's the
    privacy contract; this read honors it.
    """
    now = now or datetime.now(timezone.utc)
    window_start_day = (now - timedelta(days=window_days)).date()

    distinct_agents = (
        db.query(func.count(func.distinct(FleetPing.salt_hash)))
        .filter(FleetPing.last_seen_day >= window_start_day)
        .scalar()
    )
    return {
        "window_days": window_days,
        "distinct_agents_seen": int(distinct_agents or 0),
    }
