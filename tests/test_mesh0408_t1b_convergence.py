"""mesh_0408 T1-B\u2032 \u2014 per-assignment convergence observability (RED-proof gate suite).

The adversarial test in this file (``test_one_noisy_healthy_loop_cannot_mask_three_silently_failing``)
is the CENTRAL gate for this phase: it reproduces the exact scenario that
produced the 27-day silent outage (see
``references/reconcile-outcome-vs-event-volume-2026-08-02.md``) \u2014 one loop
that fires constantly and always succeeds, alongside several loops that fire
rarely and fail every single time. An aggregate ``success_count >
rolled_back_count`` style gate reads this fleet as healthy. This suite proves
the per-assignment replacement does not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.models import APIKey, Fleet, FleetMember, LoopPlacement, LoopRun, User
from app.services import loop_convergence as conv


def _mk_fleet(db):
    f = Fleet(id=uuid4(), owner_user_id=uuid4(), name="f", fleet_api_key_hash=f"fh-{uuid4().hex}")
    db.add(f)
    db.flush()
    return f


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


def _place(db, fleet, loop_slug, member, epoch=1, status="active"):
    p = LoopPlacement(
        id=uuid4(),
        fleet_id=fleet.id,
        loop_key=loop_slug,
        member_id=member.id,
        status=status,
        placement_epoch=epoch,
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


# \u2500\u2500 assignment_convergence: single-assignment unit tests \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_never_attempted_is_distinguishable_from_failing(db_session):
    """Gate 3: an assignment with zero LoopRun rows must not read as FAILING."""
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()

    result = conv.assignment_convergence(
        db_session, fleet.id, "never-run-loop", m.id, desired_epoch=1
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

    result = conv.assignment_convergence(db_session, fleet.id, "beacon", m.id, desired_epoch=1)
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

    result = conv.assignment_convergence(db_session, fleet.id, "stuck-loop", m.id, desired_epoch=1)
    assert result.state == conv.ConvergenceState.FAILING
    assert result.consecutive_failures == 5
    assert result.to_dict()["healthy"] is False


def test_drifting_when_latest_pass_is_behind_desired_epoch(db_session):
    """Placement moved to epoch 2; the only recorded run passed at epoch 1."""
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    _run(db_session, fleet, m, "moved-loop", "success", epoch=1)
    db_session.commit()

    result = conv.assignment_convergence(db_session, fleet.id, "moved-loop", m.id, desired_epoch=2)
    assert result.state == conv.ConvergenceState.DRIFTING
    assert result.to_dict()["healthy"] is False


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


# \u2500\u2500 fleet_convergence: THE CENTRAL ADVERSARIAL GATE \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500


def test_one_noisy_healthy_loop_cannot_mask_three_silently_failing(db_session):
    """THE gate. Reproduces the 27-day postmortem fleet composition:

      * 1 beacon loop firing every 3 minutes, always succeeding \u2014 480
        successes/day, exactly the volume that satisfied v1's proposed
        (never-shipped) aggregate success>rolled_back gate.
      * 3 daily loops that each fail EVERY run they've ever made.

    An aggregate success-vs-failure ratio across the whole fleet reads this
    as overwhelmingly healthy (480 successes vs 3 failures \u2192 99.4% success).
    fleet_convergence() must report RED regardless, because it never
    aggregates across assignments \u2014 it flags any FAILING row directly.
    """
    fleet = _mk_fleet(db_session)
    beacon_member = _mk_member(db_session, fleet, host="beacon-host")
    daily_member = _mk_member(db_session, fleet, host="daily-host")
    db_session.commit()

    _place(db_session, fleet, "p4-loop-proof", beacon_member, epoch=1)
    _place(db_session, fleet, "daily-report-a", daily_member, epoch=1)
    _place(db_session, fleet, "daily-report-b", daily_member, epoch=1)
    _place(db_session, fleet, "daily-report-c", daily_member, epoch=1)
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
    _place(db_session, fleet, "loop-a", m, epoch=1)
    _place(db_session, fleet, "loop-b", m, epoch=1)
    db_session.commit()
    _run(db_session, fleet, m, "loop-a", "success", epoch=1)
    _run(db_session, fleet, m, "loop-b", "success", epoch=1)
    db_session.commit()

    result = conv.fleet_convergence(db_session, fleet.id)
    assert result["status"] == "green"
    assert result["failing_count"] == 0


def test_never_attempted_does_not_flip_status_to_red_alone(db_session):
    """A brand-new placement with no runs yet is NOT the same as a red fleet
    \u2014 it is surfaced via never_attempted_count, not folded into failing."""
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    _place(db_session, fleet, "brand-new-loop", m, epoch=1)
    db_session.commit()

    result = conv.fleet_convergence(db_session, fleet.id)
    assert result["status"] == "green"
    assert result["failing_count"] == 0
    assert result["never_attempted_count"] == 1


def test_draining_and_removed_placements_are_excluded(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    _place(db_session, fleet, "gone-loop", m, epoch=1, status="removed")
    _place(db_session, fleet, "draining-loop", m, epoch=1, status="draining")
    db_session.commit()

    result = conv.fleet_convergence(db_session, fleet.id)
    assert result["assignment_count"] == 0
