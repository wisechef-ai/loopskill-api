"""B.7 — Canary pipeline.

Six-stage state machine for promoting a drafted patch to 100% of fleet:

    STATIC → PROPERTY → SHADOW → CANARY 1% → 10% → 50% → 100%

Auto-rollback triggers (any tier):
  - incident rate > 1.5x baseline for 4 consecutive hours
  - new error signatures appear at >3x prior rate
  - p95 latency degrades >50%

This module owns the engine + state machine + rollback decision logic.
The metrics provider is mocked behind a `MetricsProvider` Protocol so the
real telemetry source can be wired in once `install_events` carry the
right fields. The state model and rollback math are real and tested.

Also exposes `GET /api/stats/patches?period=7d` for transparency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import PatchCandidate

# ── State machine ──────────────────────────────────────────────────────


class Stage(str, Enum):
    STATIC = "static"
    PROPERTY = "property"
    SHADOW = "shadow"
    CANARY_1 = "canary_1"
    CANARY_10 = "canary_10"
    CANARY_50 = "canary_50"
    ROLLED_OUT = "rolled_out"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"


# Per-stage forward transition. Anything not in this map is terminal.
NEXT_STAGE: dict[Stage, Stage] = {
    Stage.STATIC: Stage.PROPERTY,
    Stage.PROPERTY: Stage.SHADOW,
    Stage.SHADOW: Stage.CANARY_1,
    Stage.CANARY_1: Stage.CANARY_10,
    Stage.CANARY_10: Stage.CANARY_50,
    Stage.CANARY_50: Stage.ROLLED_OUT,
}

# Minimum dwell time at each canary tier per plan §B.7
DWELL: dict[Stage, timedelta] = {
    Stage.CANARY_1: timedelta(hours=24),
    Stage.CANARY_10: timedelta(hours=72),
    Stage.CANARY_50: timedelta(days=7),
}


# ── Rollback rules ─────────────────────────────────────────────────────


@dataclass
class StageMetrics:
    """A snapshot the metrics provider returns for one stage window."""

    incident_rate: float  # incidents / install / hour
    baseline_rate: float  # pre-patch baseline for the same skill
    new_signatures: int  # signatures unseen in baseline window
    baseline_new_sig_rate: float
    p95_latency_ms: float
    baseline_p95_ms: float
    sustained_hours: int  # consecutive hours of elevated incidents


def should_rollback(m: StageMetrics) -> tuple[bool, str | None]:
    """Return True if canary metrics indicate a rollback is warranted."""
    if m.baseline_rate > 0:
        ratio = m.incident_rate / m.baseline_rate
        if ratio > 1.5 and m.sustained_hours >= 4:
            return True, f"incident rate {ratio:.2f}x for {m.sustained_hours}h"
    if m.baseline_new_sig_rate > 0:
        sig_ratio = m.new_signatures / m.baseline_new_sig_rate
        if sig_ratio > 3.0:
            return True, f"new signatures at {sig_ratio:.2f}x prior rate"
    if m.baseline_p95_ms > 0:
        lat_ratio = m.p95_latency_ms / m.baseline_p95_ms
        if lat_ratio > 1.5:
            return True, f"p95 latency degraded {lat_ratio:.2f}x"
    return False, None


# ── Metrics provider interface (real one lands with telemetry) ─────────


class MetricsProvider(Protocol):
    def metrics_for(self, candidate_id: str, stage: Stage) -> StageMetrics: ...


class StaticGate(Protocol):
    def has_regression_test(self, candidate_id: str) -> bool: ...
    def regression_test_passes(self, candidate_id: str) -> bool: ...


class PropertyGate(Protocol):
    def invariants_pass(self, candidate_id: str) -> bool: ...


class ShadowGate(Protocol):
    def shadow_diffs_clean(self, candidate_id: str) -> bool: ...


# ── Engine ─────────────────────────────────────────────────────────────


@dataclass
class Engine:
    metrics: MetricsProvider
    static_gate: StaticGate
    property_gate: PropertyGate
    shadow_gate: ShadowGate

    def step(
        self,
        candidate: PatchCandidate,
        current: Stage,
        *,
        now: datetime | None = None,
        entered_at: datetime | None = None,
    ) -> tuple[Stage, str | None]:
        """Advance one tick. Returns (new_stage, reason_if_rolled_back).

        For STATIC/PROPERTY/SHADOW the gate decides pass/fail.
        For CANARY tiers we check rollback metrics first, then dwell time.
        """
        now = now or datetime.now(UTC)

        if current == Stage.STATIC:
            if not self.static_gate.has_regression_test(str(candidate.id)):
                return Stage.REJECTED, "no regression test"
            if not self.static_gate.regression_test_passes(str(candidate.id)):
                return Stage.REJECTED, "regression test fails on new"
            return Stage.PROPERTY, None

        if current == Stage.PROPERTY:
            if not self.property_gate.invariants_pass(str(candidate.id)):
                return Stage.REJECTED, "property gate failed"
            return Stage.SHADOW, None

        if current == Stage.SHADOW:
            if not self.shadow_gate.shadow_diffs_clean(str(candidate.id)):
                return Stage.REJECTED, "shadow gate flagged drift"
            return Stage.CANARY_1, None

        if current in (Stage.CANARY_1, Stage.CANARY_10, Stage.CANARY_50):
            m = self.metrics.metrics_for(str(candidate.id), current)
            roll, reason = should_rollback(m)
            if roll:
                return Stage.ROLLED_BACK, reason
            dwell = DWELL[current]
            if entered_at and (now - entered_at) >= dwell:
                return NEXT_STAGE[current], None
            return current, None

        # ROLLED_OUT / ROLLED_BACK / REJECTED — terminal
        return current, None


# ── /api/stats/patches endpoint ────────────────────────────────────────

router = APIRouter(prefix="/api/stats", tags=["stats"])


_PERIOD_MAP = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}


@router.get("/patches")
def get_patch_stats(
    period: str = Query("7d", pattern="^(24h|7d|30d)$"),
    db: Session = Depends(get_db),
) -> dict:
    """Return patch success/failure stats for the requested time period."""
    cutoff = datetime.now(UTC) - _PERIOD_MAP[period]
    rows = (
        db.query(PatchCandidate.status, func.count(PatchCandidate.id))
        .filter(PatchCandidate.created_at >= cutoff)
        .group_by(PatchCandidate.status)
        .all()
    )
    by_status = {status: int(n) for status, n in rows}
    return {
        "period": period,
        "drafted": by_status.get("drafted", 0)
        + by_status.get("canary", 0)
        + by_status.get("rolled_out", 0)
        + by_status.get("rolled_back", 0),
        "canary": by_status.get("canary", 0),
        "rolled_out": by_status.get("rolled_out", 0),
        "rolled_back": by_status.get("rolled_back", 0),
        "rejected": by_status.get("rejected", 0),
        "pending": by_status.get("pending", 0),
        "by_status": by_status,
    }
