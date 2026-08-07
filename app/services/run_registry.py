"""fleetos_1607 Phase D — fleet run registry (honest event semantics).

Upgrades the shipped LoopRun facts table (activate_0701, at-least-once emitter,
no dedup) into an HONEST registry (§0 #11):

  * dedup on (loop, tick, attempt, epoch) — duplicate delivery cannot inflate a
    pass rate.
  * `unknown` is a first-class outcome distinct from pass/fail — a killed /
    missing-terminal run is `unknown`, never a silent success.
  * `stale-epoch` runs (a run stamped with a placement epoch older than the
    loop's current live epoch) are FLAGGED and EXCLUDED from pass numerators,
    but counted in the health denominator.

Server receipt time + member_seq dominate wall clocks for ordering. Pass rate =
passes / (total - unknown - stale). This is the data the trust ledger reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import LoopPlacement, LoopRun
from app.services.synthetic_runs import RunCounts, classify_run_synthetic

# Canonical outcome vocabulary. `unknown` is distinct from fail.
OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_UNKNOWN = "unknown"
_TERMINAL_OUTCOMES = {OUTCOME_PASS, OUTCOME_FAIL}


@dataclass
class IngestResult:
    run_id: str | None
    deduped: bool
    stale_epoch: bool
    outcome: str


def _current_live_epoch(db: Session, fleet_id: UUID, loop_slug: str) -> int | None:
    """The epoch of the loop's current live placement, or None if unplaced."""
    pl = (
        db.query(LoopPlacement)
        .filter(
            LoopPlacement.fleet_id == fleet_id,
            LoopPlacement.loop_key == loop_slug,
            LoopPlacement.status.in_(("assigned", "active", "draining")),
        )
        .order_by(LoopPlacement.placement_epoch.desc())
        .first()
    )
    return pl.placement_epoch if pl else None


def ingest_run(
    db: Session,
    *,
    member_id: UUID,
    fleet_id: UUID,
    loop_slug: str,
    tick_id: str,
    attempt: int,
    placement_epoch: int | None,
    outcome: str,
    member_seq: int | None = None,
    cost_usd: float | None = None,
    duration_seconds: int | None = None,
    detail: str | None = None,
) -> IngestResult:
    """Ingest a loop run with honest dedup + stale-epoch flagging.

    Dedup: if a run already exists for (loop, tick, attempt, epoch), this is a
    duplicate delivery — no new row, no double count. Stale-epoch: if the run's
    epoch is behind the loop's current live epoch, it's flagged (excluded from
    pass numerators). Outcome is normalized; anything not pass/fail becomes
    `unknown`.
    """
    norm_outcome = outcome if outcome in _TERMINAL_OUTCOMES else OUTCOME_UNKNOWN

    # Dedup check on the honest axis.
    dup = (
        db.query(LoopRun)
        .filter(
            LoopRun.loop_slug == loop_slug,
            LoopRun.tick_id == tick_id,
            LoopRun.attempt == attempt,
            LoopRun.placement_epoch == placement_epoch,
        )
        .first()
    )
    if dup is not None:
        return IngestResult(
            run_id=str(dup.id), deduped=True, stale_epoch=dup.stale_epoch, outcome=dup.outcome
        )

    # Stale-epoch: a run whose epoch is behind the current live epoch.
    live_epoch = _current_live_epoch(db, fleet_id, loop_slug)
    is_stale = placement_epoch is not None and live_epoch is not None and placement_epoch < live_epoch

    run = LoopRun(
        id=uuid4(),
        member_id=member_id,
        fleet_id=fleet_id,
        loop_slug=loop_slug,
        instance_key=tick_id,  # keep the legacy column populated
        tick_id=tick_id,
        attempt=attempt,
        placement_epoch=placement_epoch,
        member_seq=member_seq,
        outcome=norm_outcome,
        stale_epoch=bool(is_stale),
        # mesh_0408 W4: origin frozen at ingest, same as stale_epoch above —
        # a run is an immutable fact, so it is classified once.
        is_synthetic=classify_run_synthetic(db, loop_slug=loop_slug, member_id=member_id),
        cost_usd=cost_usd,
        duration_seconds=duration_seconds,
        detail=detail,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return IngestResult(run_id=str(run.id), deduped=False, stale_epoch=bool(is_stale), outcome=norm_outcome)


@dataclass
class PassRate:
    loop_slug: str
    total: int
    passes: int
    fails: int
    unknown: int
    stale: int
    # mesh_0408 W4: how many of ``total`` were LoopSkill's own beacon/CI
    # traffic. Reported alongside total everywhere, never folded into it.
    synthetic: int = 0

    @property
    def external(self) -> int:
        return self.total - self.synthetic

    @property
    def counted(self) -> int:
        """Runs that count toward the pass rate (exclude unknown + stale)."""
        return self.total - self.unknown - self.stale

    @property
    def pass_rate(self) -> float | None:
        """passes / (total - unknown - stale). None when nothing counts."""
        c = self.counted
        return (self.passes / c) if c > 0 else None


def pass_rate_for_loop(db: Session, fleet_id: UUID, loop_slug: str) -> PassRate:
    """Compute the HONEST pass rate for a loop.

    unknown and stale-epoch runs are excluded from the numerator AND from the
    counted denominator — a killed run or a zombie's stale run can neither pass
    nor fail the loop, but they ARE visible in the health denominator (total).
    """
    rows = (
        db.query(
            LoopRun.outcome,
            LoopRun.stale_epoch,
            LoopRun.is_synthetic,
            func.count(LoopRun.id),
        )
        .filter(LoopRun.fleet_id == fleet_id, LoopRun.loop_slug == loop_slug)
        .group_by(LoopRun.outcome, LoopRun.stale_epoch, LoopRun.is_synthetic)
        .all()
    )
    total = passes = fails = unknown = stale = synthetic = 0
    for outcome, is_stale, is_synthetic, n in rows:
        total += n
        if is_synthetic:
            synthetic += n
        if is_stale:
            stale += n
            continue  # stale runs don't count as pass/fail
        if outcome == OUTCOME_PASS:
            passes += n
        elif outcome == OUTCOME_FAIL:
            fails += n
        else:
            unknown += n
    return PassRate(
        loop_slug=loop_slug,
        total=total,
        passes=passes,
        fails=fails,
        unknown=unknown,
        stale=stale,
        synthetic=synthetic,
    )


def fleet_state(db: Session, fleet_id: UUID, limit: int = 200) -> dict[str, Any]:
    """Materialize a bounded fleet-state summary for the manager surface.

    Per-loop honest pass rates over the fleet's runs. Bounded pagination (limit)
    so a huge fleet can't blow the response. This is the query the trust ledger
    and the manager `loopskill_fleet_state` tool read.
    """
    slugs = db.query(LoopRun.loop_slug).filter(LoopRun.fleet_id == fleet_id).distinct().limit(limit).all()
    loops = []
    for (slug,) in slugs:
        pr = pass_rate_for_loop(db, fleet_id, slug)
        loops.append(
            {
                "loop_slug": slug,
                "total": pr.total,
                # mesh_0408 W4 — the adoption-bearing split. A caller reading
                # only ``total`` for this fleet would count LoopSkill's own
                # */3min beacon as a customer running loops.
                "synthetic": pr.synthetic,
                "external": pr.external,
                "passes": pr.passes,
                "fails": pr.fails,
                "unknown": pr.unknown,
                "stale_epoch": pr.stale,
                "pass_rate": pr.pass_rate,
            }
        )
    runs = RunCounts(
        total=sum(lo["total"] for lo in loops),
        synthetic=sum(lo["synthetic"] for lo in loops),
    )
    return {
        "fleet_id": str(fleet_id),
        "loop_count": len(loops),
        "runs": runs.to_dict(),
        "loops": loops,
    }


def trust_ledger_view(db: Session, fleet_id: UUID) -> dict[str, Any]:
    """Trust-ledger parity view: the same honest numbers the local trust-log reads.

    Returns aggregate counted-pass-rate across the fleet, with unknown/stale
    excluded from numerators (trust-log.sh API-mode parity — §0 D.4).
    """
    state = fleet_state(db, fleet_id)
    counted = sum((lo["passes"] + lo["fails"]) for lo in state["loops"])
    passes = sum(lo["passes"] for lo in state["loops"])
    return {
        "fleet_id": str(fleet_id),
        "counted_runs": counted,
        "passes": passes,
        "aggregate_pass_rate": (passes / counted) if counted > 0 else None,
        "excluded_unknown": sum(lo["unknown"] for lo in state["loops"]),
        "excluded_stale": sum(lo["stale_epoch"] for lo in state["loops"]),
        # mesh_0408 W4: the ledger reports run volume, so it reports origin.
        "runs": state["runs"],
    }
