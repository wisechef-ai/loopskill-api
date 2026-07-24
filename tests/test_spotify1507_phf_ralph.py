"""spotify_1507 Phase F — Ralph-loop detector tests.

The plan's Phase F gate: "Ralph detector (unit-tested)". Covers the pure
detector logic + the DB wrapper, with RED-proofs that a converging loop and a
churning loop are NOT flagged (no false Ralph).
"""

from __future__ import annotations

import uuid

import pytest

from app.services.ralph_detector import detect_ralph_loops, find_ralph_loops


def _run(member, slug, instance, outcome, accepted=False):
    return {
        "member_id": member,
        "loop_slug": slug,
        "instance_key": instance,
        "outcome": outcome,
        "accepted_change": accepted,
    }


# ── pure detector ────────────────────────────────────────────────────────────


def test_stuck_loop_is_flagged():
    """5+ runs, same outcome, zero accepted_change → Ralph."""
    runs = [_run("m1", "atomic-habits", "i1", "no_change") for _ in range(6)]
    findings = detect_ralph_loops(runs)
    assert len(findings) == 1
    f = findings[0]
    assert f["loop_slug"] == "atomic-habits"
    assert f["run_count"] == 6
    assert f["dominant_outcome"] == "no_change"
    assert f["accepted_changes"] == 0


def test_below_min_runs_not_flagged():
    """Only 4 runs (< min 5) → not enough evidence, not Ralph."""
    runs = [_run("m1", "s", "i1", "no_change") for _ in range(4)]
    assert detect_ralph_loops(runs) == []


def test_converging_loop_not_flagged_redproof():
    """RED-proof: a loop that made progress (>=1 accepted_change) is NOT Ralph,
    even with many same-outcome runs — flagging it would cry wolf on a working
    agent."""
    runs = [_run("m1", "s", "i1", "ok") for _ in range(5)]
    runs.append(_run("m1", "s", "i1", "ok", accepted=True))  # progress!
    findings = detect_ralph_loops(runs)
    assert findings == [], "a loop that accepted a change must never be flagged Ralph"


def test_churning_varied_outcomes_not_flagged_redproof():
    """RED-proof: a loop retrying through VARIED transient outcomes (no single
    dominant outcome) is legitimately churning, not stuck — not Ralph."""
    outcomes = ["timeout", "rate_limited", "error", "timeout", "network", "error"]
    runs = [_run("m1", "s", "i1", o) for o in outcomes]
    # 6 runs, but most-common outcome ('timeout'/'error') is only 2/6 < 0.8.
    assert detect_ralph_loops(runs) == []


def test_separate_instances_not_merged():
    """Same loop_slug but different instance_key are separate loops; each needs
    its own min_runs to trip."""
    runs = [_run("m1", "s", "i1", "x") for _ in range(3)]
    runs += [_run("m1", "s", "i2", "x") for _ in range(3)]
    # neither instance reaches min_runs=5
    assert detect_ralph_loops(runs) == []


def test_multiple_members_each_evaluated():
    runs = [_run("m1", "s", "i1", "stuck") for _ in range(5)]
    runs += [_run("m2", "s", "i1", "stuck") for _ in range(5)]
    findings = detect_ralph_loops(runs)
    assert len(findings) == 2
    assert {f["member_id"] for f in findings} == {"m1", "m2"}


def test_worst_offender_first():
    runs = [_run("m1", "small", "i1", "x") for _ in range(5)]
    runs += [_run("m1", "big", "i2", "x") for _ in range(9)]
    findings = detect_ralph_loops(runs)
    assert findings[0]["loop_slug"] == "big"  # most runs first
    assert findings[0]["run_count"] == 9


# ── DB wrapper ───────────────────────────────────────────────────────────────


def test_find_ralph_loops_db(db_session):
    from app.models import LoopRun

    fleet_id = uuid.uuid4()
    member_id = uuid.uuid4()
    for _ in range(6):
        db_session.add(
            LoopRun(
                id=uuid.uuid4(),
                member_id=member_id,
                fleet_id=fleet_id,
                loop_slug="dreaming",
                instance_key="nightly",
                outcome="no_change",
                accepted_change=False,
            )
        )
    db_session.commit()

    findings = find_ralph_loops(db_session, fleet_id)
    assert len(findings) == 1
    assert findings[0]["loop_slug"] == "dreaming"
    assert findings[0]["run_count"] == 6


def test_find_ralph_loops_db_ignores_other_fleets(db_session):
    from app.models import LoopRun

    fleet_a = uuid.uuid4()
    fleet_b = uuid.uuid4()
    member = uuid.uuid4()
    for _ in range(6):
        db_session.add(
            LoopRun(
                id=uuid.uuid4(),
                member_id=member,
                fleet_id=fleet_b,
                loop_slug="s",
                instance_key="i",
                outcome="x",
                accepted_change=False,
            )
        )
    db_session.commit()
    # Querying fleet_a sees nothing from fleet_b.
    assert find_ralph_loops(db_session, fleet_a) == []
