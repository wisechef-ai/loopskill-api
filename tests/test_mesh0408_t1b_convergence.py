"""mesh_0408 T1-B′ — per-assignment convergence observability (RED-proof gate suite).

The adversarial test in this file (``test_one_noisy_healthy_loop_cannot_mask_three_silently_failing``)
is the CENTRAL gate for this phase: it reproduces the exact scenario that
produced the 27-day silent outage (see
``references/reconcile-outcome-vs-event-volume-2026-08-02.md``) — one loop
that fires constantly and always succeeds, alongside several loops that fire
rarely and fail every single time. An aggregate ``success_count >
rolled_back_count`` style gate reads this fleet as healthy. This suite proves
the per-assignment replacement does not.

mesh_0408 W4 closes the OTHER half of the same illusion, tested from
``TestSilenceIsNotHealth`` down: a loop that has never fired, or that fired for
months and then went quiet, used to read green forever. Every assertion in
this file now supplies a schedule, because after W4 an assignment whose
expected firing rate is unknown is UNKNOWN_SCHEDULE — not green.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models import APIKey, Fleet, FleetMember, LoopManifest, LoopPlacement, LoopRun, User
from app.services import loop_convergence as conv

# Schedules used across the suite, named so the intent of each deadline
# assertion is readable at the call site.
EVERY_3_MIN = "*/3 * * * *"
DAILY_AT_9 = "0 9 * * *"
WEEKLY_MONDAY = "0 9 * * 1"


def _mk_fleet(db):
    f = Fleet(id=uuid4(), owner_user_id=uuid4(), name="f", fleet_api_key_hash=f"fh-{uuid4().hex}")
    db.add(f)
    db.flush()
    return f


def _declare(db, fleet, loop_id, schedule=EVERY_3_MIN):
    """Declare the LoopManifest that gives ``loop_id`` a knowable firing rate.

    Scoped exactly the way ``declare_loop`` stamps them (owner_user_id from
    the fleet, org_id NULL for a personal fleet) so these tests exercise the
    real resolution path in ``_schedules_for_placements``, not a shortcut.
    """
    m = LoopManifest(
        id=uuid4(),
        loop_id=loop_id,
        owner_user_id=fleet.owner_user_id,
        org_id=fleet.org_id,
        schedule=schedule,
        prompt="do the thing",
    )
    db.add(m)
    db.flush()
    return m


def _mk_member(db, fleet, host="a"):
    u = User(id=uuid4(), display_name="u")
    db.add(u)
    db.flush()
    k = APIKey(id=uuid4(), user_id=u.id, key_prefix=uuid4().hex[:8], key_hash=f"h-{uuid4().hex}", name="k")
    db.add(k)
    db.flush()
    m = FleetMember(
        id=uuid4(), fleet_id=fleet.id, host=host, profile="default", skills_dir="~/.h", api_key_id=k.id
    )
    db.add(m)
    db.flush()
    return m


def _place(db, fleet, loop_slug, member, epoch=1, status="active", assigned_at=None):
    p = LoopPlacement(
        id=uuid4(),
        fleet_id=fleet.id,
        loop_key=loop_slug,
        member_id=member.id,
        status=status,
        placement_epoch=epoch,
        created_at=assigned_at or datetime.now(UTC),
    )
    db.add(p)
    db.flush()
    return p


def _run(db, fleet, member, loop_slug, outcome, *, epoch=1, when=None):
    db.add(
        LoopRun(
            id=uuid4(),
            member_id=member.id,
            fleet_id=fleet.id,
            loop_slug=loop_slug,
            instance_key=uuid4().hex,
            outcome=outcome,
            placement_epoch=epoch,
            created_at=when or datetime.now(UTC),
        )
    )


# ── assignment_convergence: single-assignment unit tests ────────────────────────────


def test_never_attempted_is_distinguishable_from_failing(db_session):
    """Gate 3: an assignment with zero LoopRun rows must not read as FAILING.

    Still true after W4 — but only INSIDE the grace window. The freshly
    assigned placement below is seconds old, so NEVER_ATTEMPTED is the honest
    answer; ``TestSilenceIsNotHealth`` covers what happens when it ages.
    """
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()

    result = conv.assignment_convergence(
        db_session,
        fleet.id,
        "never-run-loop",
        m.id,
        desired_epoch=1,
        schedule=DAILY_AT_9,
        assigned_at=datetime.now(UTC),
    )
    assert result.state == conv.ConvergenceState.NEVER_ATTEMPTED
    assert result.state != conv.ConvergenceState.FAILING
    assert result.consecutive_failures == 0
    assert result.convergence_age_seconds is None  # honest: nothing to measure age against
    assert result.to_dict()["healthy"] is False


def test_converged_assignment(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    _run(db_session, fleet, m, "beacon", "success", epoch=1)
    db_session.commit()

    result = conv.assignment_convergence(
        db_session, fleet.id, "beacon", m.id, desired_epoch=1, schedule=EVERY_3_MIN
    )
    assert result.state == conv.ConvergenceState.CONVERGED
    assert result.consecutive_failures == 0
    assert result.to_dict()["healthy"] is True


def test_single_stuck_loop_detected_via_consecutive_failures(db_session):
    """A loop that fails every run gets a non-zero consecutive_failures on
    ITS OWN row, independent of any other loop's volume."""
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    for _ in range(5):
        _run(db_session, fleet, m, "stuck-loop", "failure", epoch=1)
    db_session.commit()

    # Deliberately NO schedule: a loop that ran and errored is FAILING on the
    # evidence of its own runs, so this verdict must never be downgraded to
    # UNKNOWN_SCHEDULE just because nobody declared a firing rate.
    result = conv.assignment_convergence(db_session, fleet.id, "stuck-loop", m.id, desired_epoch=1)
    assert result.state == conv.ConvergenceState.FAILING
    assert result.consecutive_failures == 5
    assert result.to_dict()["healthy"] is False


def test_drifting_when_latest_pass_is_behind_desired_epoch(db_session):
    """Placement moved to epoch 2 a moment ago; the only run passed at epoch 1.

    DRIFTING is the honest answer only INSIDE the new epoch's grace window,
    which is why ``assigned_at`` (the instant epoch 2 began) is supplied here.
    ``TestSilenceIsJudgedForTheCurrentPlacementEpoch`` is the other side of the
    same rule: once that window closes the row goes OVERDUE and the FLEET goes
    red. Asserting only ``healthy is False`` on this row is what let the
    fleet-level hole ship — so the fleet status is asserted here too.
    """
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    now = datetime.now(UTC)
    _place(db_session, fleet, "moved-loop", m, epoch=2, assigned_at=now - timedelta(minutes=1))
    _declare(db_session, fleet, "moved-loop", EVERY_3_MIN)
    _run(db_session, fleet, m, "moved-loop", "success", epoch=1, when=now - timedelta(minutes=1))
    db_session.commit()

    result = conv.assignment_convergence(
        db_session,
        fleet.id,
        "moved-loop",
        m.id,
        desired_epoch=2,
        schedule=EVERY_3_MIN,
        assigned_at=now - timedelta(minutes=1),
        now=now,
    )
    assert result.state == conv.ConvergenceState.DRIFTING
    assert result.to_dict()["healthy"] is False
    # A drifting row is transient-by-design and must NOT alarm the fleet while
    # the new epoch is still inside its own deadline.
    fleet_view = conv.fleet_convergence(db_session, fleet.id, now=now)
    assert fleet_view["status"] == "green"
    assert fleet_view["drifting_count"] == 1


def test_convergence_age_measures_since_last_pass_not_since_latest_failure(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    now = datetime.now(UTC)
    _run(db_session, fleet, m, "flapper", "success", epoch=1, when=now - timedelta(hours=10))
    _run(db_session, fleet, m, "flapper", "failure", epoch=1, when=now - timedelta(hours=1))
    db_session.commit()

    result = conv.assignment_convergence(
        db_session, fleet.id, "flapper", m.id, desired_epoch=1, now=now
    )
    assert result.state == conv.ConvergenceState.FAILING
    assert result.consecutive_failures == 1
    # age is since the failure streak began chasing the LAST pass, i.e. ~10h,
    # not ~1h (which would hide how long this has actually been broken).
    assert result.convergence_age_seconds == 36000.0


# ── fleet_convergence: THE CENTRAL ADVERSARIAL GATE ─────────────────────────────────


def test_one_noisy_healthy_loop_cannot_mask_three_silently_failing(db_session):
    """THE gate. Reproduces the 27-day postmortem fleet composition:

      * 1 beacon loop firing every 3 minutes, always succeeding — 480
        successes/day, exactly the volume that satisfied v1's proposed
        (never-shipped) aggregate success>rolled_back gate.
      * 3 daily loops that each fail EVERY run they've ever made.

    An aggregate success-vs-failure ratio across the whole fleet reads this
    as overwhelmingly healthy (480 successes vs 3 failures → 99.4% success).
    fleet_convergence() must report RED regardless, because it never
    aggregates across assignments — it flags any FAILING row directly.
    """
    fleet = _mk_fleet(db_session)
    beacon_member = _mk_member(db_session, fleet, host="beacon-host")
    daily_member = _mk_member(db_session, fleet, host="daily-host")
    db_session.commit()

    _place(db_session, fleet, "p4-loop-proof", beacon_member, epoch=1)
    _declare(db_session, fleet, "p4-loop-proof", EVERY_3_MIN)
    for slug in ("daily-report-a", "daily-report-b", "daily-report-c"):
        _place(db_session, fleet, slug, daily_member, epoch=1)
        _declare(db_session, fleet, slug, DAILY_AT_9)
    db_session.commit()

    now = datetime.now(UTC)

    # The noisy healthy beacon: 480 successes over the last 24h (every 3min).
    for i in range(480):
        _run(
            db_session,
            fleet,
            beacon_member,
            "p4-loop-proof",
            "success",
            epoch=1,
            when=now - timedelta(minutes=3 * i),
        )

    # Three daily loops, each failing every run it has ever made.
    for slug in ("daily-report-a", "daily-report-b", "daily-report-c"):
        for i in range(7):  # a week of daily fires, all failed
            _run(
                db_session,
                fleet,
                daily_member,
                slug,
                "failure",
                epoch=1,
                when=now - timedelta(days=i),
            )
    db_session.commit()

    # Sanity: an aggregate success>rolled_back-style count WOULD read green.
    total_success = 480
    total_failure = 3 * 7
    assert total_success > total_failure, "the adversarial setup must satisfy the deleted aggregate gate"

    result = conv.fleet_convergence(db_session, fleet.id, now=now)

    assert result["status"] == "red", (
        "one healthy beacon must not mask three silently-failing daily loops"
    )
    assert result["failing_count"] == 3
    assert result["assignment_count"] == 4

    failing_slugs = {
        a["loop_key"] for a in result["assignments"] if a["state"] == "failing"
    }
    assert failing_slugs == {"daily-report-a", "daily-report-b", "daily-report-c"}

    beacon_row = next(a for a in result["assignments"] if a["loop_key"] == "p4-loop-proof")
    assert beacon_row["state"] == "converged"
    assert beacon_row["healthy"] is True


def test_fleet_convergence_green_when_every_assignment_healthy(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    for slug in ("loop-a", "loop-b"):
        _place(db_session, fleet, slug, m, epoch=1)
        _declare(db_session, fleet, slug, EVERY_3_MIN)
    db_session.commit()
    _run(db_session, fleet, m, "loop-a", "success", epoch=1)
    _run(db_session, fleet, m, "loop-b", "success", epoch=1)
    db_session.commit()

    result = conv.fleet_convergence(db_session, fleet.id)
    assert result["status"] == "green"
    assert result["status_reasons"] == []
    assert result["failing_count"] == 0


def test_never_attempted_inside_its_grace_window_does_not_flip_status_to_red(db_session):
    """A brand-new placement with no runs yet is NOT the same as a red fleet
    — it is surfaced via never_attempted_count, not folded into failing.

    mesh_0408 W4 keeps that intention and gives it an expiry. The placement
    below is a DAILY loop assigned one minute ago; its schedule-derived
    deadline (3 x 86400s, clamped by the 7-day ceiling) is days away, so
    green is the honest answer *right now* and the response says exactly when
    that stops being true. The companion test
    ``test_never_attempted_past_its_deadline_flips_the_fleet_red`` is the
    other side of the same rule — before W4 there was no other side.
    """
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    now = datetime.now(UTC)
    _place(db_session, fleet, "brand-new-loop", m, epoch=1, assigned_at=now - timedelta(minutes=1))
    _declare(db_session, fleet, "brand-new-loop", DAILY_AT_9)
    db_session.commit()

    result = conv.fleet_convergence(db_session, fleet.id, now=now)
    assert result["status"] == "green"
    assert result["failing_count"] == 0
    assert result["never_attempted_count"] == 1
    assert result["overdue_count"] == 0

    row = result["assignments"][0]
    assert row["state"] == "never_attempted"
    # The grace is NOT a constant — it is 3 x the schedule's own interval,
    # and the row carries the resulting deadline so no client has to guess.
    assert row["expected_interval_seconds"] == pytest.approx(86400.0)
    assert row["overdue_deadline_at"] is not None
    assert row["seconds_until_overdue"] == pytest.approx(
        conv.silence_grace_seconds(86400.0) - 60.0, abs=2.0
    )


def test_draining_and_removed_placements_are_excluded(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    _place(db_session, fleet, "gone-loop", m, epoch=1, status="removed")
    _place(db_session, fleet, "draining-loop", m, epoch=1, status="draining")
    db_session.commit()

    result = conv.fleet_convergence(db_session, fleet.id)
    assert result["assignment_count"] == 0


# ── mesh_0408 W4: SILENCE IS NOT HEALTH ─────────────────────────────────────────────
#
# THE defect these close: before W4, `status = "red" if failing else "green"`.
# Only a loop that RAN and FAILED could redden the fleet, so a loop that never
# fired at all — production's actual state, 3 of 4 assignments never_attempted
# — reported green indefinitely. hub.md Q-021 flagged that as a design
# question rather than a defect precisely because the naive fix (age
# NEVER_ATTEMPTED into FAILING on a constant) is wrong in both directions: an
# hour of silence is a catastrophe for a */3min beacon and a non-event for a
# weekly report. The deadline therefore comes from the loop's OWN schedule.


class TestSilenceIsNotHealth:
    def test_never_attempted_past_its_deadline_flips_the_fleet_red(self, db_session):
        """THE W4 GATE (RED-proof scenario 1 from the phase brief).

        A `*/3 * * * *` beacon, assigned 3 hours ago, that has never emitted a
        single run. Pre-W4 this fleet reported ``green``. It is 3 hours of
        silence on a loop that should have fired 60 times.
        """
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        now = datetime.now(UTC)
        _place(db_session, fleet, "beacon", m, epoch=1, assigned_at=now - timedelta(hours=3))
        _declare(db_session, fleet, "beacon", EVERY_3_MIN)
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id, now=now)

        assert result["status"] != "green", (
            "a */3min loop silent for 3 hours must not render as a green fleet"
        )
        assert result["status"] == "red"
        assert result["status_reasons"] == ["overdue"]
        assert result["overdue_count"] == 1
        # OVERDUE, not FAILING: nothing errored, nothing ran. Different
        # diagnosis (check the scheduler / the host), different repair.
        assert result["failing_count"] == 0
        row = result["assignments"][0]
        assert row["state"] == "overdue"
        assert row["healthy"] is False
        assert row["last_run_at"] is None
        assert row["seconds_until_overdue"] < 0

    def test_a_weekly_loop_assigned_two_days_ago_is_fine(self, db_session):
        """The other direction of the same rule — no false alarm.

        This is what a hard-coded staleness threshold gets wrong: the portal's
        1-hour constant rendered every healthy daily/weekly loop as stale.
        """
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        now = datetime.now(UTC)
        _place(db_session, fleet, "weekly-report", m, epoch=1, assigned_at=now - timedelta(days=2))
        _declare(db_session, fleet, "weekly-report", WEEKLY_MONDAY)
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id, now=now)
        assert result["status"] == "green"
        assert result["overdue_count"] == 0
        assert result["assignments"][0]["state"] == "never_attempted"
        assert result["assignments"][0]["expected_interval_seconds"] == pytest.approx(604800.0)

    def test_the_deadline_scales_with_the_schedule_not_with_a_constant(self, db_session):
        """Same silence, same instant, two schedules, opposite verdicts.

        This is the assertion a constant threshold CANNOT satisfy in either
        direction: 3h of silence is overdue for the 3-minute loop and fine for
        the weekly one, in one fleet, in one call.
        """
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        now = datetime.now(UTC)
        assigned = now - timedelta(hours=3)
        _place(db_session, fleet, "a-fast-loop", m, epoch=1, assigned_at=assigned)
        _declare(db_session, fleet, "a-fast-loop", EVERY_3_MIN)
        _place(db_session, fleet, "b-slow-loop", m, epoch=1, assigned_at=assigned)
        _declare(db_session, fleet, "b-slow-loop", WEEKLY_MONDAY)
        db_session.commit()

        rows = {a["loop_key"]: a for a in conv.fleet_convergence(db_session, fleet.id, now=now)["assignments"]}
        assert rows["a-fast-loop"]["state"] == "overdue"
        assert rows["b-slow-loop"]["state"] == "never_attempted"

    def test_a_loop_that_ran_for_months_then_went_quiet_is_overdue_not_converged(self, db_session):
        """The second half of the same defect class.

        ``CONVERGED`` meant "the last thing it did was pass" — which is not
        the claim "it is still running". A beacon whose last successful run
        was 3 hours ago used to read converged/green forever.
        """
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        now = datetime.now(UTC)
        _place(db_session, fleet, "beacon", m, epoch=1, assigned_at=now - timedelta(days=30))
        _declare(db_session, fleet, "beacon", EVERY_3_MIN)
        for i in range(10):
            _run(
                db_session, fleet, m, "beacon", "success", epoch=1,
                when=now - timedelta(hours=3, minutes=3 * i),
            )
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id, now=now)
        assert result["status"] == "red"
        assert result["overdue_count"] == 1
        row = result["assignments"][0]
        assert row["state"] == "overdue"
        assert row["last_outcome"] == "success"  # it passed — and then stopped
        assert row["silent_since"] == row["last_run_at"]
        # Every timestamp this endpoint emits carries an offset. A naive
        # string would be parsed by the client as local time, and a staleness
        # readout that is silently hours off is the same lie in a new place.
        for field in ("last_run_at", "silent_since", "overdue_deadline_at"):
            assert row[field].endswith("+00:00"), f"{field} must be offset-aware"

    def test_failing_beats_overdue_so_an_error_is_never_reported_as_silence(self, db_session):
        """A loop that ran, failed, and then went quiet reads FAILING.

        Precedence matters diagnostically: there IS an error to read.
        """
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        now = datetime.now(UTC)
        _place(db_session, fleet, "broken", m, epoch=1, assigned_at=now - timedelta(days=30))
        _declare(db_session, fleet, "broken", EVERY_3_MIN)
        _run(db_session, fleet, m, "broken", "failure", epoch=1, when=now - timedelta(hours=3))
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id, now=now)
        assert result["status"] == "red"
        assert result["failing_count"] == 1
        assert result["overdue_count"] == 0
        assert result["status_reasons"] == ["failing"]


class TestSilenceIsJudgedForTheCurrentPlacementEpoch:
    """The placement-epoch axis of "silence is not health" (was W4-I2).

    W4 shipped with silence measured from the newest run for the
    ``(fleet, member, loop)`` triple, **whatever epoch that run belonged to**.
    So a placement that moved to epoch 2 and never fired again kept renewing
    its own grace period off an epoch-1 run: the row read DRIFTING (correct)
    but DRIFTING is not a red state, so the FLEET rendered green forever —
    the exact silent-green shape W4 exists to delete, one state over.

    The rule now: silence for the CURRENT epoch is measured from the newest
    run **of that epoch**, and from the epoch's own start when it has never
    run. A superseded epoch's run is evidence about a placement that no
    longer exists and buys the live one nothing.
    """

    def test_a_superseded_epochs_run_does_not_keep_the_fleet_green(self, db_session):
        """THE gate for this finding, at FLEET level.

        Epoch 2 was placed 30 days ago and has never produced a run. The only
        run in the table passed one minute ago — under epoch 1. Pre-fix:
        ``status == "green"``, ``status_reasons == []``.
        """
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        now = datetime.now(UTC)
        _place(db_session, fleet, "moved-loop", m, epoch=2, assigned_at=now - timedelta(days=30))
        _declare(db_session, fleet, "moved-loop", EVERY_3_MIN)
        _run(db_session, fleet, m, "moved-loop", "success", epoch=1, when=now - timedelta(minutes=1))
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id, now=now)

        assert result["status"] == "red", (
            "a placement whose live epoch has never run must not render green "
            "on the strength of a superseded epoch's run"
        )
        assert result["status_reasons"] == ["overdue"]
        assert result["overdue_count"] == 1
        assert result["drifting_count"] == 0

        row = result["assignments"][0]
        assert row["state"] == "overdue"
        assert row["healthy"] is False
        assert row["desired_epoch"] == 2
        assert row["observed_epoch"] == 1  # the drift is still legible on the row
        # Silence is measured from when epoch 2 began, NOT from the epoch-1
        # run — otherwise the deadline is a minute away instead of 30 days past.
        assert row["seconds_until_overdue"] < 0
        assert row["silent_since"] != row["last_run_at"]

    def test_a_placement_that_just_moved_is_drifting_not_overdue(self, db_session):
        """The distinction the fix must preserve: brief drift is not a stuck
        placement, exactly as NEVER_ATTEMPTED is not OVERDUE. Same data as the
        test above, one field different — the epoch began a minute ago."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        now = datetime.now(UTC)
        _place(db_session, fleet, "moved-loop", m, epoch=2, assigned_at=now - timedelta(minutes=1))
        _declare(db_session, fleet, "moved-loop", EVERY_3_MIN)
        _run(db_session, fleet, m, "moved-loop", "success", epoch=1, when=now - timedelta(minutes=1))
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id, now=now)
        assert result["status"] == "green"
        assert result["drifting_count"] == 1
        assert result["overdue_count"] == 0
        assert result["assignments"][0]["state"] == "drifting"

    def test_drifting_is_bounded_by_the_schedules_own_deadline_not_a_constant(self, db_session):
        """Same drift, same instant, two schedules, opposite verdicts — the
        assertion a constant drift timeout could not satisfy. Both placements
        moved 3 hours ago and neither new epoch has run."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        now = datetime.now(UTC)
        moved = now - timedelta(hours=3)
        for slug, schedule in (("a-fast-loop", EVERY_3_MIN), ("b-slow-loop", WEEKLY_MONDAY)):
            _place(db_session, fleet, slug, m, epoch=2, assigned_at=moved)
            _declare(db_session, fleet, slug, schedule)
            _run(db_session, fleet, m, slug, "success", epoch=1, when=moved - timedelta(minutes=1))
        db_session.commit()

        rows = {
            a["loop_key"]: a
            for a in conv.fleet_convergence(db_session, fleet.id, now=now)["assignments"]
        }
        assert rows["a-fast-loop"]["state"] == "overdue"
        assert rows["b-slow-loop"]["state"] == "drifting"

    def test_a_run_at_the_current_epoch_clears_the_drift(self, db_session):
        """Control for the two tests above: once the live epoch actually runs,
        the same 30-day-old placement is CONVERGED and the fleet is green. If
        this ever fails, the fix has over-corrected into permanent drift."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        now = datetime.now(UTC)
        _place(db_session, fleet, "moved-loop", m, epoch=2, assigned_at=now - timedelta(days=30))
        _declare(db_session, fleet, "moved-loop", EVERY_3_MIN)
        _run(db_session, fleet, m, "moved-loop", "success", epoch=1, when=now - timedelta(days=29))
        _run(db_session, fleet, m, "moved-loop", "success", epoch=2, when=now - timedelta(minutes=1))
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id, now=now)
        assert result["status"] == "green"
        assert result["assignments"][0]["state"] == "converged"

    def test_a_legacy_run_with_no_epoch_still_counts_as_current(self, db_session):
        """Runs ingested before ``placement_epoch`` existed carry NULL. They
        must keep counting for the live epoch — treating them as superseded
        would flip every pre-Phase-A fleet red on a data artefact."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        now = datetime.now(UTC)
        _place(db_session, fleet, "legacy-loop", m, epoch=3, assigned_at=now - timedelta(days=30))
        _declare(db_session, fleet, "legacy-loop", EVERY_3_MIN)
        _run(db_session, fleet, m, "legacy-loop", "success", epoch=None, when=now - timedelta(minutes=1))
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id, now=now)
        assert result["status"] == "green"
        assert result["assignments"][0]["state"] == "converged"

    def test_no_reference_point_for_the_live_epoch_is_loud_not_green(self, db_session):
        """Direct caller, superseded run, and no assignment time at all: the
        module cannot say when the live epoch began, so it must not certify
        health. Same policy as an underivable schedule."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        _run(db_session, fleet, m, "moved-loop", "success", epoch=1)
        db_session.commit()

        result = conv.assignment_convergence(
            db_session, fleet.id, "moved-loop", m.id, desired_epoch=2, schedule=EVERY_3_MIN
        )
        assert result.state == conv.ConvergenceState.UNKNOWN_SCHEDULE
        assert result.schedule_status == conv.ScheduleStatus.NO_REFERENCE_TIME
        assert result.to_dict()["healthy"] is False


class TestUnknownScheduleIsNeverGreen:
    """An un-judgeable loop must be loud, never quietly healthy."""

    def test_undeclared_schedule_is_its_own_state_not_green(self, db_session):
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        # Placement exists; NO LoopManifest declares it. The member is never
        # even told when to run this loop.
        _place(db_session, fleet, "orphan-loop", m, epoch=1)
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id)
        assert result["status"] == "red"
        assert result["status_reasons"] == ["unknown_schedule"]
        assert result["unknown_schedule_count"] == 1
        row = result["assignments"][0]
        assert row["state"] == "unknown_schedule"
        assert row["schedule_status"] == "undeclared"
        assert row["overdue_deadline_at"] is None

    def test_unparseable_schedule_is_distinguishable_from_undeclared(self, db_session):
        """Both are non-green, but they are DIFFERENT repairs: one is a
        missing manifest, the other is a typo in a manifest that exists."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        _place(db_session, fleet, "typo-loop", m, epoch=1)
        _declare(db_session, fleet, "typo-loop", "every other thursday-ish")
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id)
        assert result["status"] == "red"
        row = result["assignments"][0]
        assert row["state"] == "unknown_schedule"
        assert row["schedule_status"] == "unparseable"
        assert row["schedule"] == "every other thursday-ish"

    def test_a_passing_run_does_not_buy_a_green_without_a_schedule(self, db_session):
        """The subtle one. A loop that passed 10 seconds ago still cannot be
        certified healthy if nothing declares how often it should fire — the
        next run might be due in a minute or in a month, and "it passed once"
        is not "it is running". Green here would be the exact shape of the
        pre-W4 lie, just one state over."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        _place(db_session, fleet, "undeclared-but-passing", m, epoch=1)
        _run(db_session, fleet, m, "undeclared-but-passing", "success", epoch=1)
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id)
        assert result["status"] != "green"
        assert result["assignments"][0]["state"] == "unknown_schedule"

    def test_a_disabled_manifest_does_not_supply_a_schedule(self, db_session):
        """Scoping guard: `_schedules_for_placements` filters enabled==True,
        so a disabled manifest must not quietly certify a placement."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        _place(db_session, fleet, "switched-off", m, epoch=1)
        man = _declare(db_session, fleet, "switched-off", EVERY_3_MIN)
        man.enabled = False
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet.id)
        assert result["assignments"][0]["schedule_status"] == "undeclared"
        assert result["status"] == "red"

    def test_another_owners_manifest_cannot_supply_the_schedule(self, db_session):
        """Tenant guard. Two fleets, two owners, the SAME loop_key. Fleet B's
        placement must not pick up fleet A's manifest — that would let one
        tenant's declaration certify another tenant's loop as healthy.

        Per trap V2 the two principals are asserted DISTINCT below, so this
        cannot pass by both sides resolving to the same owner.
        """
        fleet_a = _mk_fleet(db_session)
        fleet_b = _mk_fleet(db_session)
        assert fleet_a.owner_user_id != fleet_b.owner_user_id, "void test: shared owner"
        assert fleet_a.id != fleet_b.id
        m_b = _mk_member(db_session, fleet_b)
        db_session.commit()

        _declare(db_session, fleet_a, "shared-key-loop", EVERY_3_MIN)  # A's declaration
        _place(db_session, fleet_b, "shared-key-loop", m_b, epoch=1)  # B's placement
        db_session.commit()

        result = conv.fleet_convergence(db_session, fleet_b.id)
        assert result["assignments"][0]["schedule_status"] == "undeclared"
        assert result["assignments"][0]["state"] == "unknown_schedule"


class TestSilencePolicyIsRetunable:
    """Q-021 is Adam's design call, so the policy must be trivial to move."""

    def test_grace_is_multiplier_x_interval_clamped_to_floor_and_ceiling(self):
        # A 1-hour loop: 3 x 3600 sits between the floor and the ceiling.
        assert conv.silence_grace_seconds(3600.0) == 10800.0
        # A 3-minute beacon: 3 x 180 = 540s would be twitchy, so the floor
        # (15 min ~ 5 missed ticks) takes over.
        assert conv.silence_grace_seconds(180.0) == conv.SILENCE_GRACE_FLOOR_SECONDS
        # A monthly loop: 3 months of silence is not an alert anybody reads,
        # so the 7-day ceiling takes over.
        assert conv.silence_grace_seconds(30 * 86400.0) == conv.SILENCE_GRACE_CEILING_SECONDS

    def test_the_policy_is_published_in_the_response(self, db_session):
        """The client renders the SERVER's rule; it never invents a threshold.
        Publishing the constants is what makes that checkable from outside."""
        fleet = _mk_fleet(db_session)
        db_session.commit()
        policy = conv.fleet_convergence(db_session, fleet.id)["silence_policy"]
        assert policy == {
            "grace_multiplier": conv.SILENCE_GRACE_MULTIPLIER,
            "grace_floor_seconds": conv.SILENCE_GRACE_FLOOR_SECONDS,
            "grace_ceiling_seconds": conv.SILENCE_GRACE_CEILING_SECONDS,
        }
