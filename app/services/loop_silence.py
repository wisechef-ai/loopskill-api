"""mesh_0408 W4 — the SILENCE POLICY: how long quiet is still healthy.

Split out of :mod:`app.services.loop_convergence` (W4b) so the policy is one
readable unit rather than a third of a larger module: everything here answers
one question — *given a loop's declared schedule and the last sign of life it
showed, is it late yet?* — and nothing here knows what a placement, an epoch or
a fleet is. :mod:`app.services.loop_convergence` re-exports every public name
below, so this split moved no API.

Q-021 asked "should NEVER_ATTEMPTED age into FAILING?" and was flagged a DESIGN
question, not a defect. The answer: it ages into its own state (OVERDUE, not
FAILING — "never ran and is late" is a different repair than "ran and broke"),
on a deadline derived from the loop's own schedule rather than a constant. A
constant cannot work in either direction: an hour of silence is a catastrophe
for a ``*/3min`` beacon and a non-event for a weekly report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from app.services.schedule_interval import UnparseableSchedule, parse_schedule_interval

__all__ = [
    "SILENCE_GRACE_CEILING_SECONDS",
    "SILENCE_GRACE_FLOOR_SECONDS",
    "SILENCE_GRACE_MULTIPLIER",
    "ScheduleStatus",
    "Silence",
    "evaluate_silence",
    "silence_grace_seconds",
]

# These three constants ARE the policy. They are deliberately named, exported
# in the API response under ``silence_policy``, and referenced nowhere else, so
# retuning the aggressiveness of the gate is a one-line change with no
# behavioural archaeology.
#
#   deadline = silent_since + clamp(MULTIPLIER * expected_interval,
#                                   FLOOR, CEILING)
#
# MULTIPLIER 3 = "three firings missed in a row". The floor keeps a very
# frequent loop from alarming on one slow cycle (a */3min beacon gets 15
# minutes ≈ 5 ticks). The ceiling keeps a rare loop from being un-alarmable
# forever (a monthly loop is overdue after 7 days of silence, not 3 months);
# for a weekly loop the ceiling lands on exactly one missed period.
SILENCE_GRACE_MULTIPLIER = 3.0
SILENCE_GRACE_FLOOR_SECONDS = 900.0  # 15 minutes
SILENCE_GRACE_CEILING_SECONDS = 604800.0  # 7 days


def silence_grace_seconds(expected_interval_seconds: float) -> float:
    """How long silence is tolerated for a loop firing every N seconds."""
    return min(
        SILENCE_GRACE_CEILING_SECONDS,
        max(SILENCE_GRACE_FLOOR_SECONDS, SILENCE_GRACE_MULTIPLIER * expected_interval_seconds),
    )


class ScheduleStatus(str, Enum):
    """Why (or whether) a silence deadline could be computed."""

    PARSED = "parsed"
    # No enabled LoopManifest declares this placement's loop_key in the
    # fleet's scope — the member is not even told when to run it.
    UNDECLARED = "undeclared"
    # A schedule string exists but no firing rate can be derived from it.
    UNPARSEABLE = "unparseable"
    # A schedule exists but there is no reference point to measure silence
    # from (nothing has run under the live placement epoch AND no known
    # assignment time).
    NO_REFERENCE_TIME = "no_reference_time"


@dataclass
class Silence:
    """The schedule-derived silence verdict for one assignment."""

    schedule: str | None
    status: ScheduleStatus
    expected_interval_seconds: float | None
    silent_since: datetime | None
    deadline: datetime | None
    seconds_until_overdue: float | None
    overdue: bool


def evaluate_silence(
    schedule: str | None,
    silent_since: datetime | None,
    now: datetime,
) -> Silence:
    """Turn (schedule, last-sign-of-life) into an OVERDUE verdict.

    Never raises and never guesses: an unparseable or undeclared schedule, or a
    missing reference point, comes back with ``overdue=False`` and a non-PARSED
    status, which the caller MUST translate into UNKNOWN_SCHEDULE rather than
    into health. Judging silence requires knowing the expected firing rate;
    without it, saying "green" anyway is exactly the lie W4 exists to remove.
    """
    try:
        interval = parse_schedule_interval(schedule)
    except UnparseableSchedule:
        status = ScheduleStatus.UNDECLARED if not schedule else ScheduleStatus.UNPARSEABLE
        return Silence(schedule, status, None, silent_since, None, None, False)

    if silent_since is None:
        return Silence(schedule, ScheduleStatus.NO_REFERENCE_TIME, interval, None, None, None, False)

    silent_since = _aware(silent_since)
    deadline = silent_since + timedelta(seconds=silence_grace_seconds(interval))
    slack = (deadline - now).total_seconds()
    return Silence(
        schedule=schedule,
        status=ScheduleStatus.PARSED,
        expected_interval_seconds=interval,
        silent_since=silent_since,
        deadline=deadline,
        seconds_until_overdue=slack,
        overdue=slack < 0,
    )


def _aware(dt: datetime) -> datetime:
    """Normalize a DB-read timestamp to UTC-aware.

    These columns are ``DateTime(timezone=True)`` and always hold UTC, but
    SQLite hands them back naive — and subtracting a naive from an aware
    datetime raises, so this is load-bearing, not cosmetic.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
