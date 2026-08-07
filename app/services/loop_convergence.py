"""mesh_0408 T1-B′ — per-assignment convergence observability.

**The aggregate gate this module replaces was never actually shipped as code
in this repo — it existed only as v1's PROPOSED design** (mesh_0408 plan
§0.1, §3 Phase T1-B′): "healthy iff fleet-wide success_count >
rolled_back_count". Both adversarial council seats destroyed that design
before a single line of it was written, because it is exactly the shape of
the failure documented in
``references/reconcile-outcome-vs-event-volume-2026-08-02.md``: LoopSkill's
own healthy ``*/3min`` beacon loop (``p4-loop-proof``) emits roughly 480
successes/day — more than enough for ANY fleet-wide success-vs-failure
ratio to read green while several OTHER daily loops fail on every single
run. Deletion is therefore the first fix (plan §0.1, musk-5-step): this
module intentionally contains NO aggregate success/rolled_back ratio,
anywhere, ever. If a future change reintroduces one, it reintroduces the
27-day silent outage class.

Convergence is computed **per assignment** — one ``(fleet_id, loop_key,
member_id)`` triple — from data that already exists:

* **desired revision** — ``LoopPlacement.placement_epoch`` (the CAS-guarded
  monotonic epoch of the current live placement).
* **observed revision** — the ``placement_epoch`` stamped on the most
  recent ``LoopRun`` for that assignment (fleetos_1607 Phase D honest event
  contract — the run registry already refuses to let a stale-epoch run count
  as current).
* **consecutive-failure count** — how many of the most recent runs, walking
  backward from now, failed to pass — stopping at the first pass. A single
  stuck loop shows a non-zero count on ITS OWN row; it is never blended with
  a healthy sibling's success volume.
* **NEVER_ATTEMPTED** — an assignment with a live LoopPlacement but zero
  LoopRun rows. Distinct from FAILING: a loop that has never fired is not
  the same defect class as one that fires and fails every time, and
  conflating the two (the failure the plan's gate #3 names) hides a
  bootstrap defect behind "no data == fine."

* **OVERDUE** (mesh_0408 W4) — the assignment has been SILENT past a deadline
  derived from its own schedule. Covers all three halves of the same defect
  class: a loop that was assigned and never fired, a loop that fired happily
  for months and then went quiet, and a placement that MOVED and whose new
  epoch never fired. See ":ref:`silence`" below.

Fleet-level status is RED if **any** tracked assignment is FAILING, OVERDUE or
UNKNOWN_SCHEDULE — full stop, no averaging, no ratio. One stuck loop cannot be
masked by N healthy ones.

.. _silence:

**Silence is not health (mesh_0408 W4).** Before W4 this module rendered
NEVER_ATTEMPTED as green forever: production ran 3-of-4 assignments
``never_attempted`` and the fleet reported green indefinitely. A constant
threshold cannot fix that — an hour of silence is a catastrophe for a
``*/3min`` beacon and a non-event for a weekly report. So the deadline is
derived from the loop's OWN schedule
(:func:`app.services.schedule_interval.parse_schedule_interval`) and the
policy is three named, retunable constants in
:mod:`app.services.loop_silence`, re-exported here. The reference point for
silence is the last run of the CURRENT placement epoch if there is one, and the
instant that epoch began if there is not, so a freshly-assigned (or freshly
re-placed) loop still gets its grace period — the correct intention the pre-W4
behaviour encoded, now with an expiry.

Measuring from the current epoch is what makes the claim true along the
placement axis too. Measured from "the newest run of any epoch", a placement
that moved and never fired again renewed its own grace period off its
predecessor's traffic forever: the row read DRIFTING, DRIFTING is not a red
state, and the fleet stayed green. Every transient state in this module is now
reachable only INSIDE a deadline, and becomes OVERDUE once that deadline
passes. A transient state with no expiry is the bug, not a nuance of it.

If a schedule cannot be resolved (undeclared, or declared but unparseable) the
assignment lands in **UNKNOWN_SCHEDULE**, which is NOT green. Judging silence
requires knowing the expected firing rate; without it this module cannot
certify health, and saying "green" anyway is exactly the lie W4 exists to
remove.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Fleet, LoopManifest, LoopPlacement, LoopRun

# mesh_0408 W4b: the silence policy lives in its own module — one unit
# answering one question ("is this loop late yet?"), and the piece Adam
# retunes for Q-021. Re-exported here because it is part of THIS module's
# published surface: the API response echoes the three constants under
# ``silence_policy`` and callers import ScheduleStatus from here. The split
# moved code, not API.
from app.services.loop_silence import (  # noqa: F401  — re-exported surface
    SILENCE_GRACE_CEILING_SECONDS,
    SILENCE_GRACE_FLOOR_SECONDS,
    SILENCE_GRACE_MULTIPLIER,
    ScheduleStatus,
    evaluate_silence,
    silence_grace_seconds,
)

# Placement statuses that should actively be converging right now. Mirrors
# app.loop_assignment_routes._SCHEDULABLE_STATUSES — a draining/removed
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
    # The live placement epoch has produced no run of its own — a placement
    # moved and the new owner hasn't reported yet — but the new epoch is still
    # INSIDE its schedule-derived deadline. Distinct from FAILING (nothing is
    # actually erroring) and from CONVERGED (the live desired state isn't
    # reflected). Bounded: past the deadline this becomes OVERDUE, which is
    # red. Before that bound existed, a placement could drift silently forever.
    DRIFTING = "drifting"
    # Silent past its schedule-derived deadline. Distinct from FAILING on
    # purpose: FAILING means the loop ran and errored (read the logs);
    # OVERDUE means nothing ran at all (check the scheduler, the host, the
    # placement). Different diagnosis, different repair.
    OVERDUE = "overdue"
    # No derivable schedule, so silence cannot be judged. Loud and non-green
    # by construction — the alternative is rendering an un-judgeable loop as
    # healthy, which is the bug this module exists to remove.
    UNKNOWN_SCHEDULE = "unknown_schedule"


# States that make the fleet non-green, most-actionable first (this order is
# the order of ``status_reasons`` in the response). NEVER_ATTEMPTED and
# DRIFTING stay out because both are now bounded: each is reachable ONLY while
# the assignment is inside its schedule-derived deadline, and each becomes
# OVERDUE the moment that deadline passes. A transient state with no expiry is
# how silence rendered as health in the first place; neither has one now.
_RED_STATES = (
    ConvergenceState.FAILING,
    ConvergenceState.OVERDUE,
    ConvergenceState.UNKNOWN_SCHEDULE,
)


@dataclass
class AssignmentConvergence:
    """Per-assignment convergence snapshot — the unit this phase makes visible."""

    fleet_id: str
    member_id: str
    loop_key: str
    desired_epoch: int
    observed_epoch: int | None
    state: ConvergenceState
    consecutive_failures: int
    last_outcome: str | None
    last_run_at: str | None
    # None only for NEVER_ATTEMPTED — there is no age to report when nothing
    # has ever run. For every other state this is seconds since the last
    # PASSING run, or (if the assignment has attempts but has never passed)
    # seconds since its earliest observed attempt — i.e. "how long has this
    # been failing", never silently coerced to 0.
    convergence_age_seconds: float | None

    # ── mesh_0408 W4 — server-computed silence verdict ───────────────────────────────
    # These exist so a client (portal/mesh.astro, the CLI, an alerting hook)
    # renders a state the SERVER decided, instead of re-deriving staleness
    # from a hard-coded threshold it cannot make schedule-aware.
    schedule: str | None
    schedule_status: ScheduleStatus
    expected_interval_seconds: float | None
    # The instant silence started being measured from: the last run, or the
    # placement's assignment time when nothing has ever run.
    silent_since: str | None
    # The instant this assignment turns/turned OVERDUE. None when no deadline
    # is derivable (schedule_status != parsed).
    overdue_deadline_at: str | None
    # Signed: positive = this much slack left, negative = this far past due.
    seconds_until_overdue: float | None

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
            "schedule": self.schedule,
            "schedule_status": self.schedule_status.value,
            "expected_interval_seconds": self.expected_interval_seconds,
            "silent_since": self.silent_since,
            "overdue_deadline_at": self.overdue_deadline_at,
            "seconds_until_overdue": self.seconds_until_overdue,
            "healthy": self.state == ConvergenceState.CONVERGED,
        }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _current_epoch_runs(runs: list[LoopRun], desired_epoch: int) -> list[LoopRun]:
    """The subset of ``runs`` that speaks for the CURRENTLY desired epoch.

    A run stamped with a SUPERSEDED epoch converged a placement that no longer
    exists. Before this filter existed, silence was measured from the newest
    run of ANY epoch, so a placement that moved and then never fired again kept
    buying itself a fresh grace period off its predecessor's traffic: the row
    read DRIFTING (correct) but DRIFTING is not a red state, so the FLEET read
    green indefinitely — the same silent-green shape this module exists to
    delete, one state over.

    ``placement_epoch IS NULL`` counts as current: those rows predate
    fleetos_1607 Phase A, and treating a missing stamp as "superseded" would
    flip every legacy fleet red on a data artefact rather than on evidence.
    """
    return [r for r in runs if r.placement_epoch is None or r.placement_epoch >= desired_epoch]


def assignment_convergence(
    db: Session,
    fleet_id: UUID,
    loop_key: str,
    member_id: UUID,
    *,
    desired_epoch: int,
    schedule: str | None = None,
    assigned_at: datetime | None = None,
    now: datetime | None = None,
    run_window: int = DEFAULT_RUN_WINDOW,
) -> AssignmentConvergence:
    """Compute convergence for ONE (fleet, loop, member) assignment.

    Reads only — no writes, no aggregation across assignments. Callers that
    need a fleet-wide view compose this per placement (see
    :func:`fleet_convergence`); this function never itself blends more than
    one assignment's data, which is the property the deleted aggregate gate
    violated.

    Args:
        schedule: the loop's declared schedule expression (``LoopManifest.
            schedule``). Omitting it is NOT a way to get a green answer — a
            missing schedule yields ``UNKNOWN_SCHEDULE``, because silence
            cannot be judged without an expected firing rate.
        assigned_at: when the CURRENT epoch's placement row was created — i.e.
            when this epoch began, since every tracked placement row is minted
            by the transition that created its epoch. The silence reference
            point whenever the live epoch has produced no run of its own.
            Omitting it is not a way to get a green answer either: with a
            superseded-epoch run and no assignment time there is no reference
            point for the live epoch, which yields
            ``NO_REFERENCE_TIME`` → ``UNKNOWN_SCHEDULE``.
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
        # Nothing has ever run. Silence is measured from the assignment
        # itself: brand-new gets grace, weeks-old does not.
        silence = evaluate_silence(schedule, assigned_at, now)
        if silence.status is not ScheduleStatus.PARSED:
            state = ConvergenceState.UNKNOWN_SCHEDULE
        elif silence.overdue:
            state = ConvergenceState.OVERDUE
        else:
            state = ConvergenceState.NEVER_ATTEMPTED
        return AssignmentConvergence(
            fleet_id=str(fleet_id),
            member_id=str(member_id),
            loop_key=loop_key,
            desired_epoch=desired_epoch,
            observed_epoch=None,
            state=state,
            consecutive_failures=0,
            last_outcome=None,
            last_run_at=None,
            convergence_age_seconds=None,
            schedule=silence.schedule,
            schedule_status=silence.status,
            expected_interval_seconds=silence.expected_interval_seconds,
            silent_since=_iso(silence.silent_since),
            overdue_deadline_at=_iso(silence.deadline),
            seconds_until_overdue=silence.seconds_until_overdue,
        )

    latest = runs[0]

    # Runs that speak for the CURRENT placement epoch. A run stamped with a
    # SUPERSEDED epoch is evidence about a placement that no longer exists —
    # see _current_epoch_runs for why letting it speak was a silent-green hole.
    current_epoch_runs = _current_epoch_runs(runs, desired_epoch)

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

    # Silence for an assignment that HAS run is measured from its most recent
    # CURRENT-EPOCH run — the other half of the same defect class as
    # NEVER_ATTEMPTED: a beacon that ran happily for months and then went quiet
    # used to read CONVERGED forever, because "the last thing it did was pass"
    # is not the same claim as "it is still running".
    #
    # When the live epoch has never run, the reference point is when that epoch
    # BEGAN, which is exactly ``assigned_at``: every tracked placement row is
    # created by the transition that minted its epoch (app/services/placement.py
    # — complete_move/force_move add a fresh row at epoch+1; begin_drain's
    # in-place bump leaves the row ``draining``, which _TRACKED_STATUSES
    # excludes). So a placement that moved 30 days ago and never fired is 30
    # days silent, not "one minute silent" because its predecessor ran.
    silent_since = current_epoch_runs[0].created_at if current_epoch_runs else assigned_at
    silence = evaluate_silence(schedule, silent_since, now)

    if consecutive_failures > 0:
        # A loop that ran and errored is FAILING regardless of the schedule —
        # this verdict needs no expected interval, so it is decided first and
        # never downgraded to UNKNOWN_SCHEDULE.
        state = ConvergenceState.FAILING
    elif silence.status is not ScheduleStatus.PARSED:
        state = ConvergenceState.UNKNOWN_SCHEDULE
    elif silence.overdue:
        # Covers both shapes of the same silence: nothing has run for a while,
        # and the live epoch has NEVER run and its deadline has passed.
        state = ConvergenceState.OVERDUE
    elif not current_epoch_runs:
        # Drift inside the live epoch's own grace window. Transient by design
        # and bounded by the branch above — the placement moved seconds ago and
        # the new owner has not had a scheduled firing yet.
        state = ConvergenceState.DRIFTING
    else:
        state = ConvergenceState.CONVERGED

    if last_converged_at is not None:
        age = max(0.0, (now - _aware(last_converged_at)).total_seconds())
    else:
        # Never converged within the window — age is measured from the
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
        # Normalized through _aware(): these columns are DateTime(timezone=True)
        # and always hold UTC, but SQLite hands them back naive. Emitting a
        # naive ISO string would make a client parse it as ITS OWN local time —
        # and a staleness display that is silently hours off is the same class
        # of lie this module exists to remove.
        last_run_at=_iso(_aware(latest.created_at) if latest.created_at else None),
        convergence_age_seconds=age,
        schedule=silence.schedule,
        schedule_status=silence.status,
        expected_interval_seconds=silence.expected_interval_seconds,
        silent_since=_iso(silence.silent_since),
        overdue_deadline_at=_iso(silence.deadline),
        seconds_until_overdue=silence.seconds_until_overdue,
    )


def fleet_convergence(
    db: Session,
    fleet_id: UUID,
    *,
    now: datetime | None = None,
    run_window: int = DEFAULT_RUN_WINDOW,
) -> dict[str, Any]:
    """Per-assignment convergence for every actively-tracked placement in a fleet.

    THE FIX (plan §3 Phase T1-B′): overall ``status`` is ``"red"`` iff ANY
    tracked assignment is FAILING. There is no ratio, no aggregate count, no
    threshold a noisy healthy loop can satisfy on a broken sibling's behalf.
    One stuck daily loop is visible on its own row and flips the fleet
    status, regardless of how many other loops (including a beacon firing
    every 3 minutes) are healthy.

    mesh_0408 W4 widens that: ``status`` is also red when any assignment is
    OVERDUE (silent past its schedule-derived deadline) or UNKNOWN_SCHEDULE
    (no derivable deadline at all). ``status_reasons`` names which of the
    three it was, so red is never a mystery. The counts stay separate — a
    fleet nobody has bootstrapped and a fleet whose loops are erroring need
    different work.
    """
    placements = (
        db.query(LoopPlacement)
        .filter(LoopPlacement.fleet_id == fleet_id, LoopPlacement.status.in_(_TRACKED_STATUSES))
        .order_by(LoopPlacement.loop_key)
        .all()
    )

    schedules = _schedules_for_placements(db, fleet_id, placements)

    assignments = [
        assignment_convergence(
            db,
            fleet_id,
            p.loop_key,
            p.member_id,
            desired_epoch=p.placement_epoch,
            schedule=schedules.get(p.loop_key),
            assigned_at=p.created_at,
            now=now,
            run_window=run_window,
        )
        for p in placements
    ]

    by_state: dict[ConvergenceState, list[AssignmentConvergence]] = {}
    for a in assignments:
        by_state.setdefault(a.state, []).append(a)

    failing = by_state.get(ConvergenceState.FAILING, [])
    never_attempted = by_state.get(ConvergenceState.NEVER_ATTEMPTED, [])
    drifting = by_state.get(ConvergenceState.DRIFTING, [])
    overdue = by_state.get(ConvergenceState.OVERDUE, [])
    unknown_schedule = by_state.get(ConvergenceState.UNKNOWN_SCHEDULE, [])

    status_reasons = [s.value for s in _RED_STATES if by_state.get(s)]
    status = "red" if status_reasons else "green"

    return {
        "fleet_id": str(fleet_id),
        "status": status,
        "status_reasons": status_reasons,
        "assignment_count": len(assignments),
        "failing_count": len(failing),
        "never_attempted_count": len(never_attempted),
        "drifting_count": len(drifting),
        "overdue_count": len(overdue),
        "unknown_schedule_count": len(unknown_schedule),
        # The policy that produced every ``overdue_deadline_at`` below —
        # echoed so a client renders the server's rule instead of inventing
        # its own threshold, and so retuning it is visible in the response.
        "silence_policy": {
            "grace_multiplier": SILENCE_GRACE_MULTIPLIER,
            "grace_floor_seconds": SILENCE_GRACE_FLOOR_SECONDS,
            "grace_ceiling_seconds": SILENCE_GRACE_CEILING_SECONDS,
        },
        "assignments": [a.to_dict() for a in assignments],
    }


def _schedules_for_placements(
    db: Session,
    fleet_id: UUID,
    placements: list[LoopPlacement],
) -> dict[str, str]:
    """Resolve each placement's declared schedule in ONE query.

    Scoping mirrors ``app.loop_assignment_routes`` exactly — declare_loop
    stamps BOTH ``owner_user_id`` and ``org_id`` from the fleet onto every
    manifest, so the read side must match both, including ``org_id IS NULL``
    for a personal fleet. Getting this wrong here would silently classify
    every loop of an org-scoped fleet as UNKNOWN_SCHEDULE.

    A loop_key with no enabled manifest is simply absent from the result, and
    the caller turns that into UNKNOWN_SCHEDULE — never into a default.
    """
    loop_keys = {p.loop_key for p in placements}
    if not loop_keys:
        return {}

    fleet = db.query(Fleet).filter(Fleet.id == fleet_id).first()
    if fleet is None:
        return {}

    q = db.query(LoopManifest).filter(
        LoopManifest.loop_id.in_(loop_keys),
        LoopManifest.enabled == True,  # noqa: E712
        LoopManifest.owner_user_id == fleet.owner_user_id,
    )
    if fleet.org_id is not None:
        q = q.filter(LoopManifest.org_id == fleet.org_id)
    else:
        q = q.filter(LoopManifest.org_id.is_(None))

    return {m.loop_id: m.schedule for m in q.all() if m.schedule}
