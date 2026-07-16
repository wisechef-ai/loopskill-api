"""tests/test_fleetos_D_run_registry.py — fleetos_1607 Phase D gate suite.

RED-proofs honest event semantics:
  * duplicate delivery on (loop, tick, attempt, epoch) does NOT inflate the pass
    rate.
  * `unknown` is distinct from pass — a killed / non-terminal run is unknown, and
    excluded from the pass numerator.
  * a stale-epoch run (epoch < the loop's current live placement epoch) is
    flagged and excluded from the pass rate, but visible in the total.
  * fleet_state + trust_ledger_view report the same honest numbers.
"""

from __future__ import annotations

from uuid import uuid4

from app.models import APIKey, Fleet, FleetMember, LoopPlacement, User
from app.services import run_registry as rr


def _mk_fleet(db):
    f = Fleet(id=uuid4(), owner_user_id=uuid4(), name="f", fleet_api_key_hash=f"fh-{uuid4().hex}")
    db.add(f)
    db.flush()
    return f


def _mk_member(db, fleet):
    u = User(id=uuid4(), display_name="u")
    db.add(u)
    db.flush()
    k = APIKey(id=uuid4(), user_id=u.id, key_prefix=uuid4().hex[:8], key_hash=f"h-{uuid4().hex}", name="k")
    db.add(k)
    db.flush()
    m = FleetMember(
        id=uuid4(), fleet_id=fleet.id, host="a", profile="default", skills_dir="~/.h", api_key_id=k.id
    )
    db.add(m)
    db.flush()
    return m


def _place(db, fleet, loop_slug, member, epoch):
    p = LoopPlacement(
        id=uuid4(),
        fleet_id=fleet.id,
        loop_key=loop_slug,
        member_id=member.id,
        status="active",
        placement_epoch=epoch,
    )
    db.add(p)
    db.flush()
    return p


# ── dedup ────────────────────────────────────────────────────────────────────


def test_duplicate_delivery_does_not_inflate(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    # same (loop, tick, attempt, epoch) reported twice
    r1 = rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-x",
        tick_id="t1",
        attempt=0,
        placement_epoch=1,
        outcome="pass",
    )
    r2 = rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-x",
        tick_id="t1",
        attempt=0,
        placement_epoch=1,
        outcome="pass",
    )
    assert r1.deduped is False
    assert r2.deduped is True
    pr = rr.pass_rate_for_loop(db_session, fleet.id, "loop-x")
    assert pr.total == 1  # not 2
    assert pr.passes == 1
    assert pr.pass_rate == 1.0


def test_different_attempts_not_deduped(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-x",
        tick_id="t1",
        attempt=0,
        placement_epoch=1,
        outcome="fail",
    )
    rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-x",
        tick_id="t1",
        attempt=1,
        placement_epoch=1,
        outcome="pass",
    )
    pr = rr.pass_rate_for_loop(db_session, fleet.id, "loop-x")
    assert pr.total == 2
    assert pr.passes == 1 and pr.fails == 1


# ── unknown outcome ──────────────────────────────────────────────────────────


def test_unknown_outcome_excluded_from_pass_rate(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-x",
        tick_id="t1",
        attempt=0,
        placement_epoch=1,
        outcome="pass",
    )
    # a killed / missing-terminal run reports a non-terminal outcome -> unknown
    r = rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-x",
        tick_id="t2",
        attempt=0,
        placement_epoch=1,
        outcome="killed",
    )
    assert r.outcome == rr.OUTCOME_UNKNOWN
    pr = rr.pass_rate_for_loop(db_session, fleet.id, "loop-x")
    assert pr.total == 2
    assert pr.unknown == 1
    assert pr.counted == 1  # only the pass counts
    assert pr.pass_rate == 1.0  # not 0.5 — unknown is not a fail


# ── stale epoch ──────────────────────────────────────────────────────────────


def test_stale_epoch_flagged_and_excluded(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    # the loop's CURRENT live placement is at epoch 3 (it was moved twice)
    _place(db_session, fleet, "loop-x", m, epoch=3)
    db_session.commit()
    # a zombie reports a run at the OLD epoch 1 -> stale
    r = rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-x",
        tick_id="t1",
        attempt=0,
        placement_epoch=1,
        outcome="pass",
    )
    assert r.stale_epoch is True
    # a current run at epoch 3
    rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-x",
        tick_id="t2",
        attempt=0,
        placement_epoch=3,
        outcome="pass",
    )
    pr = rr.pass_rate_for_loop(db_session, fleet.id, "loop-x")
    assert pr.total == 2
    assert pr.stale == 1
    assert pr.counted == 1  # the stale one is excluded
    assert pr.passes == 1  # only the live-epoch pass counts toward passes


# ── aggregate views ──────────────────────────────────────────────────────────


def test_fleet_state_and_trust_parity(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    _place(db_session, fleet, "loop-a", m, epoch=1)
    db_session.commit()
    rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-a",
        tick_id="t1",
        attempt=0,
        placement_epoch=1,
        outcome="pass",
    )
    rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-a",
        tick_id="t2",
        attempt=0,
        placement_epoch=1,
        outcome="fail",
    )
    rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-a",
        tick_id="t3",
        attempt=0,
        placement_epoch=1,
        outcome="killed",
    )  # unknown

    state = rr.fleet_state(db_session, fleet.id)
    assert state["loop_count"] == 1
    lo = state["loops"][0]
    assert lo["total"] == 3
    assert lo["passes"] == 1 and lo["fails"] == 1 and lo["unknown"] == 1
    assert lo["pass_rate"] == 0.5  # 1 pass / (1 pass + 1 fail); unknown excluded

    # trust-ledger parity — same honest numbers
    tl = rr.trust_ledger_view(db_session, fleet.id)
    assert tl["counted_runs"] == 2  # pass + fail, unknown excluded
    assert tl["passes"] == 1
    assert tl["aggregate_pass_rate"] == 0.5
    assert tl["excluded_unknown"] == 1


def test_pass_rate_none_when_all_unknown(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    rr.ingest_run(
        db_session,
        member_id=m.id,
        fleet_id=fleet.id,
        loop_slug="loop-x",
        tick_id="t1",
        attempt=0,
        placement_epoch=1,
        outcome="killed",
    )
    pr = rr.pass_rate_for_loop(db_session, fleet.id, "loop-x")
    assert pr.pass_rate is None  # nothing counts -> honest None, not 0.0
