"""mesh_0408 T1-B\u2032 \u2014 per-assignment convergence observability.

**The aggregate gate this module replaces was never actually shipped as code
in this repo \u2014 it existed only as v1's PROPOSED design** (mesh_0408 plan
\u00a70.1, \u00a73 Phase T1-B\u2032): "healthy iff fleet-wide success_count >
rolled_back_count". Both adversarial council seats destroyed that design
before a single line of it was written, because it is exactly the shape of
the failure documented in
``references/reconcile-outcome-vs-event-volume-2026-08-02.md``: LoopSkill's
own healthy ``*/3min`` beacon loop (``p4-loop-proof``) emits roughly 480
successes/day \u2014 more than enough for ANY fleet-wide success-vs-failure
ratio to read green while several OTHER daily loops fail on every single
run. Deletion is therefore the first fix (plan \u00a70.1, musk-5-step): this
module intentionally contains NO aggregate success/rolled_back ratio,
anywhere, ever. If a future change reintroduces one, it reintroduces the
27-day silent outage class.

Convergence is computed **per assignment** \u2014 one ``(fleet_id, loop_key,
member_id)`` triple \u2014 from data that already exists:

* **desired revision** \u2014 ``LoopPlacement.placement_epoch`` (the CAS-guarded
  monotonic epoch of the current live placement).
* **observed revision** \u2014 the ``placement_epoch`` stamped on the most
  recent ``LoopRun`` for that assignment (fleetos_1607 Phase D honest event
  contract \u2014 the run registry already refuses to let a stale-epoch run count
  as current).
* **consecutive-failure count** \u2014 how many of the most recent runs, walking
  backward from now, failed to pass \u2014 stopping at the first pass. A single
  stuck loop shows a non-zero count on ITS OWN row; it is never blended with
  a healthy sibling's success volume.
* **NEVER_ATTEMPTED** \u2014 an assignment with a live LoopPlacement but zero
  LoopRun rows. Distinct from FAILING: a loop that has never fired is not
  the same defect class as one that fires and fails every time, and
  conflating the two (the failure the plan's gate #3 names) hides a
  bootstrap defect behind "no data == fine."

Fleet-level status is RED if **any** tracked assignment is FAILING \u2014 full
stop, no averaging, no ratio. One stuck loop cannot be masked by N healthy
ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import LoopPlacement, LoopRun

# Placement statuses that should actively be converging right now. Mirrors
# app.loop_assignment_routes._SCHEDULABLE_STATUSES \u2014 a draining/removed
# placement is not expected to keep converging and is excluded here too.
_TRACKED_STATUSES = ("assigned", "active")

# Outcome vocabulary this module treats as "converged". Matches both the
# fleetos_1607 Phase D honest registry ("pass"/"fail"/"unknown") and the
# legacy loop-run emitter vocabulary ("success"/"failure") so a converted
# managed loop (converge_0208 P4 emitter: loopskill-emit-run.sh) and a
# run-registry-ingested loop both read correctly through the same function.
_PASS_OUTCOMES = frozenset({"pass", "success"})

# How many of the most recent runs to walk when computing consecutive
# failures / last-converged-at. Bounded so a long-lived loop with thousands
# of historical runs doesn't turn this into an unbounded table scan; large
# enough that no realistic daily-loop failure streak falls outside the
# window before this endpoint is read.
DEFAULT_RUN_WINDOW = 50


class ConvergenceState(str, Enum):
    NEVER_ATTEMPTED = "never_attempted"
    CONVERGED = "converged"
    FAILING = "failing"
    # Latest run passed, but it converged an OLDER placement epoch than the
    # one currently desired \u2014 a placement moved and the new owner hasn't
    # produced a passing run yet. Distinct from FAILING (nothing is actually
    # erroring) and from CONVERGED (the live desired state isn't reflected).
    DRIFTING = "drifting"


@dataclass
class AssignmentConvergence:
    """Per-assignment convergence snapshot \u2014 the unit this phase makes visible."""

    fleet_id: str
    member_id: str
    loop_key: str
    desired_epoch: int
    observed_epoch: int | None
    state: ConvergenceState
    consecutive_failures: int
    last_outcome: str | None
    last_run_at: str | None
    # None only for NEVER_ATTEMPTED \u2014 there is no age to report when nothing
    # has ever run. For every other state this is seconds since the last
    # PASSING run, or (if the assignment has attempts but has never passed)
    # seconds since its earliest observed attempt \u2014 i.e. "how long has this
    # been failing", never silently coerced to 0.
    convergence_age_seconds: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fleet_id": self.fleet_id,
            "member_id": self.member_id,
            "loop_key": self.loop_key,
            "desired_epoch": self.desired_epoch,
            "observed_epoch": self.observed_epoch,
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "last_outcome": self.last_outcome,
            "last_run_at": self.last_run_at,
            "convergence_age_seconds": self.convergence_age_seconds,
            "healthy": self.state == ConvergenceState.CONVERGED,
        }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def assignment_convergence(
    db: Session,
    fleet_id: UUID,
    loop_key: str,
    member_id: UUID,
    *,
    desired_epoch: int,
    now: datetime | None = None,
    run_window: int = DEFAULT_RUN_WINDOW,
) -> AssignmentConvergence:
    """Compute convergence for ONE (fleet, loop, member) assignment.

    Reads only \u2014 no writes, no aggregation across assignments. Callers that
    need a fleet-wide view compose this per placement (see
    :func:`fleet_convergence`); this function never itself blends more than
    one assignment's data, which is the property the deleted aggregate gate
    violated.
    """
    now = now or datetime.now(UTC)

    runs = (
        db.query(LoopRun)
        .filter(
            LoopRun.fleet_id == fleet_id,
            LoopRun.member_id == member_id,
            LoopRun.loop_slug == loop_key,
        )
        .order_by(LoopRun.created_at.desc())
        .limit(run_window)
        .all()
    )

    if not runs:
        return AssignmentConvergence(
            fleet_id=str(fleet_id),
            member_id=str(member_id),
            loop_key=loop_key,
            desired_epoch=desired_epoch,
            observed_epoch=None,
            state=ConvergenceState.NEVER_ATTEMPTED,
            consecutive_failures=0,
            last_outcome=None,
            last_run_at=None,
            convergence_age_seconds=None,
        )

    latest = runs[0]

    # Walk backward from the most recent run until the first PASS. The
    # count of non-passing runs before that first pass is the consecutive
    # failure streak. If nothing in the window ever passed, every run in
    # the window counts.
    consecutive_failures = 0
    last_converged_at: datetime | None = None
    for r in runs:
        if r.outcome in _PASS_OUTCOMES:
            last_converged_at = r.created_at
            break
        consecutive_failures += 1

    if consecutive_failures > 0:
        state = ConvergenceState.FAILING
    elif latest.placement_epoch is not None and latest.placement_epoch < desired_epoch:
        state = ConvergenceState.DRIFTING
    else:
        state = ConvergenceState.CONVERGED

    if last_converged_at is not None:
        age = max(0.0, (now - _aware(last_converged_at)).total_seconds())
    else:
        # Never converged within the window \u2014 age is measured from the
        # EARLIEST attempt we can see, so "how long has this been broken"
        # is honest rather than reported as 0 just because the latest
        # failing run is recent.
        earliest = runs[-1].created_at
        age = max(0.0, (now - _aware(earliest)).total_seconds())

    return AssignmentConvergence(
        fleet_id=str(fleet_id),
        member_id=str(member_id),
        loop_key=loop_key,
        desired_epoch=desired_epoch,
        observed_epoch=latest.placement_epoch,
        state=state,
        consecutive_failures=consecutive_failures,
        last_outcome=latest.outcome,
        last_run_at=latest.created_at.isoformat() if latest.created_at else None,
        convergence_age_seconds=age,
    )


def fleet_convergence(
    db: Session,
    fleet_id: UUID,
    *,
    now: datetime | None = None,
    run_window: int = DEFAULT_RUN_WINDOW,
) -> dict[str, Any]:
    """Per-assignment convergence for every actively-tracked placement in a fleet.

    THE FIX (plan \u00a73 Phase T1-B\u2032): overall ``status`` is ``"red"`` iff ANY
    tracked assignment is FAILING. There is no ratio, no aggregate count, no
    threshold a noisy healthy loop can satisfy on a broken sibling's behalf.
    One stuck daily loop is visible on its own row and flips the fleet
    status, regardless of how many other loops (including a beacon firing
    every 3 minutes) are healthy.
    """
    placements = (
        db.query(LoopPlacement)
        .filter(LoopPlacement.fleet_id == fleet_id, LoopPlacement.status.in_(_TRACKED_STATUSES))
        .order_by(LoopPlacement.loop_key)
        .all()
    )

    assignments = [
        assignment_convergence(
            db,
            fleet_id,
            p.loop_key,
            p.member_id,
            desired_epoch=p.placement_epoch,
            now=now,
            run_window=run_window,
        )
        for p in placements
    ]

    failing = [a for a in assignments if a.state == ConvergenceState.FAILING]
    never_attempted = [a for a in assignments if a.state == ConvergenceState.NEVER_ATTEMPTED]
    drifting = [a for a in assignments if a.state == ConvergenceState.DRIFTING]

    status = "red" if failing else "green"

    return {
        "fleet_id": str(fleet_id),
        "status": status,
        "assignment_count": len(assignments),
        "failing_count": len(failing),
        "never_attempted_count": len(never_attempted),
        "drifting_count": len(drifting),
        "assignments": [a.to_dict() for a in assignments],
    }
