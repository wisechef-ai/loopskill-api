"""tests/test_fleetos_A_placements.py — fleetos_1607 Phase A gate suite.

RED-proofs the placement spine:
  * epoch-CAS: a stale-epoch transition is REJECTED (never double-applies).
  * single-active invariant: never 2 active placements for one loop.
  * confirmation dedup: a replayed confirmation is counted once.
  * cooperative move: drain → confirm → activate-new lands the loop on the new
    member at a higher epoch with the old one removed.
  * force move: retires the old placement, flags forced=True, surfaces the
    per-safety-class consequence text; the MCP tool refuses without ack.
  * manager-key authz: a bare fleet-member (scope="fleet") key is 403 on manager
    tools; an operator/owner/master key is allowed.
  * capability preflight: assign onto a member missing a required secret is
    refused with the named requirement.
  * stale-member alert fires for a member whose ping is older than 3× interval.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.auth_ctx import AuthContext
from app.models import (
    APIKey,
    Fleet,
    FleetMember,
    FleetMemberLiveness,
    LoopManifest,
    User,
)
from app.services import placement as psvc
from app.services.stale_member_alert import find_stale_members
from app.mcp.tools import placement as ptool


# ── fixtures ─────────────────────────────────────────────────────────────────


def _mk_fleet(db, owner_id=None):
    owner_id = owner_id or uuid4()
    fleet = Fleet(
        id=uuid4(),
        owner_user_id=owner_id,
        name="test-fleet",
        fleet_api_key_hash=f"fh-{uuid4().hex}",
    )
    db.add(fleet)
    db.flush()
    return fleet


def _mk_member(db, fleet, host="host-a"):
    user = User(id=uuid4(), display_name=f"u-{host}")
    db.add(user)
    db.flush()
    key = APIKey(
        id=uuid4(),
        user_id=user.id,
        key_prefix=uuid4().hex[:8],
        key_hash=f"h-{uuid4().hex}",
        name=f"k-{host}",
    )
    db.add(key)
    db.flush()
    m = FleetMember(
        id=uuid4(),
        fleet_id=fleet.id,
        host=host,
        profile="default",
        skills_dir="~/.hermes/loopskill",
        api_key_id=key.id,
    )
    db.add(m)
    db.flush()
    return m


def _advertise(db, member, **provides):
    lv = FleetMemberLiveness(member_id=member.id, provides=provides or {}, reconcile_interval_seconds=300)
    db.add(lv)
    db.flush()
    return lv


def _mk_manifest(db, loop_key, **kw):
    m = LoopManifest(
        id=uuid4(),
        loop_id=loop_key,
        owner_user_id=uuid4(),
        schedule="0 9 * * *",
        prompt="do the thing",
        skills=[],
        requires=kw.get("requires", {}),
        secret_refs=kw.get("secret_refs", []),
        reserved={},
        safety_class=kw.get("safety_class", "best-effort"),
    )
    db.add(m)
    db.flush()
    return m


# ── epoch-CAS + single-active invariant ──────────────────────────────────────


def test_assign_then_second_assign_rejected(db_session):
    fleet = _mk_fleet(db_session)
    m1 = _mk_member(db_session, fleet, "a")
    _advertise(db_session, m1)
    db_session.commit()

    p1 = psvc.assign(db_session, fleet.id, "loop-x", m1.id, skip_preflight=True)
    assert p1.status == "active"
    assert psvc.active_placement_count(db_session, fleet.id, "loop-x") == 1

    m2 = _mk_member(db_session, fleet, "b")
    with pytest.raises(psvc.PlacementError) as ei:
        psvc.assign(db_session, fleet.id, "loop-x", m2.id, skip_preflight=True)
    assert ei.value.code == "already_placed"
    # still exactly one active
    assert psvc.active_placement_count(db_session, fleet.id, "loop-x") == 1


def test_stale_epoch_drain_rejected(db_session):
    fleet = _mk_fleet(db_session)
    m1 = _mk_member(db_session, fleet, "a")
    db_session.commit()
    p = psvc.assign(db_session, fleet.id, "loop-x", m1.id, skip_preflight=True)
    # someone drains at the correct epoch
    psvc.begin_drain(db_session, fleet.id, "loop-x", expected_epoch=p.placement_epoch)
    # a second writer tries to drain at the OLD (now stale) epoch → rejected
    with pytest.raises(psvc.PlacementError) as ei:
        psvc.begin_drain(db_session, fleet.id, "loop-x", expected_epoch=p.placement_epoch)
    assert ei.value.code in ("epoch_conflict", "bad_state")


def test_cooperative_move_single_active_throughout(db_session):
    fleet = _mk_fleet(db_session)
    m1 = _mk_member(db_session, fleet, "a")
    m2 = _mk_member(db_session, fleet, "b")
    db_session.commit()

    p = psvc.assign(db_session, fleet.id, "loop-x", m1.id, skip_preflight=True)
    e0 = p.placement_epoch

    draining = psvc.begin_drain(db_session, fleet.id, "loop-x", expected_epoch=e0)
    assert draining.status == "draining"
    assert draining.placement_epoch == e0 + 1
    # while draining, zero ACTIVE placements (the loop is in flight, not running)
    assert psvc.active_placement_count(db_session, fleet.id, "loop-x") == 0

    psvc.confirm_drain(db_session, draining.id, m1.id, confirmed_epoch=e0 + 1, member_seq=1)
    activated = psvc.complete_move(
        db_session, fleet.id, "loop-x", draining_epoch=e0 + 1, new_member_id=m2.id, skip_preflight=True
    )
    assert activated.member_id == m2.id
    assert activated.placement_epoch == e0 + 2
    # exactly one active again, on the new member
    assert psvc.active_placement_count(db_session, fleet.id, "loop-x") == 1


def test_complete_move_without_confirmation_refused(db_session):
    fleet = _mk_fleet(db_session)
    m1 = _mk_member(db_session, fleet, "a")
    m2 = _mk_member(db_session, fleet, "b")
    db_session.commit()
    p = psvc.assign(db_session, fleet.id, "loop-x", m1.id, skip_preflight=True)
    draining = psvc.begin_drain(db_session, fleet.id, "loop-x", expected_epoch=p.placement_epoch)
    with pytest.raises(psvc.PlacementError) as ei:
        psvc.complete_move(
            db_session,
            fleet.id,
            "loop-x",
            draining_epoch=draining.placement_epoch,
            new_member_id=m2.id,
            skip_preflight=True,
        )
    assert ei.value.code == "unconfirmed_drain"


# ── confirmation dedup ───────────────────────────────────────────────────────


def test_confirmation_dedup(db_session):
    fleet = _mk_fleet(db_session)
    m1 = _mk_member(db_session, fleet, "a")
    db_session.commit()
    p = psvc.assign(db_session, fleet.id, "loop-x", m1.id, skip_preflight=True)
    draining = psvc.begin_drain(db_session, fleet.id, "loop-x", expected_epoch=p.placement_epoch)
    c1 = psvc.confirm_drain(
        db_session, draining.id, m1.id, confirmed_epoch=draining.placement_epoch, member_seq=7
    )
    # replay with SAME member_seq → same row, not a second confirmation
    c2 = psvc.confirm_drain(
        db_session, draining.id, m1.id, confirmed_epoch=draining.placement_epoch, member_seq=7
    )
    assert c1.id == c2.id


# ── force move ───────────────────────────────────────────────────────────────


def test_force_move_retires_old_flags_forced(db_session):
    fleet = _mk_fleet(db_session)
    m1 = _mk_member(db_session, fleet, "a")
    m2 = _mk_member(db_session, fleet, "b")
    db_session.commit()
    psvc.assign(db_session, fleet.id, "loop-x", m1.id, skip_preflight=True)
    moved = psvc.force_move(db_session, fleet.id, "loop-x", m2.id, skip_preflight=True)
    assert moved.forced is True
    assert moved.member_id == m2.id
    # exactly one active, and it's the forced one
    assert psvc.active_placement_count(db_session, fleet.id, "loop-x") == 1


def test_force_move_tool_refuses_without_ack(db_session):
    owner = uuid4()
    fleet = _mk_fleet(db_session, owner_id=owner)
    m1 = _mk_member(db_session, fleet, "a")
    m2 = _mk_member(db_session, fleet, "b")
    _advertise(db_session, m2, os="linux")  # target must be a live, capable member
    _mk_manifest(db_session, "loop-x", safety_class="manual-only")
    psvc.assign(db_session, fleet.id, "loop-x", m1.id, skip_preflight=True)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=owner)
    res = ptool.loopskill_force_move(db_session, str(fleet.id), "loop-x", str(m2.id), ctx=ctx)
    assert res["error"] == "duplicate_risk_not_acknowledged"
    assert res["safety_class"] == "manual-only"
    assert "manual-only" in res["consequence"]
    # with ack it proceeds
    res2 = ptool.loopskill_force_move(
        db_session, str(fleet.id), "loop-x", str(m2.id), acknowledge_duplicate_risk=True, ctx=ctx
    )
    assert res2["force_moved"] is True
    assert res2["forced"] is True


# ── manager-key authz (the 403 RED-proof) ────────────────────────────────────


def test_member_key_cannot_call_manager_tool(db_session):
    owner = uuid4()
    fleet = _mk_fleet(db_session, owner_id=owner)
    m1 = _mk_member(db_session, fleet, "a")
    _advertise(db_session, m1)
    db_session.commit()

    # a bare fleet-MEMBER key (scope="fleet", bound to this fleet)
    member_ctx = AuthContext(scope="fleet", fleet_id=fleet.id)
    res = ptool.loopskill_assign(db_session, str(fleet.id), "loop-x", str(m1.id), ctx=member_ctx)
    assert res["code"] == 403

    res2 = ptool.loopskill_placements(db_session, str(fleet.id), ctx=member_ctx)
    assert res2["code"] == 403


def test_owner_and_operator_can_manage(db_session):
    owner = uuid4()
    fleet = _mk_fleet(db_session, owner_id=owner)
    m1 = _mk_member(db_session, fleet, "a")
    _advertise(db_session, m1)
    db_session.commit()

    owner_ctx = AuthContext(scope="user", user_id=owner)
    res = ptool.loopskill_assign(db_session, str(fleet.id), "loop-x", str(m1.id), ctx=owner_ctx)
    assert res["assigned"] is True

    # operator key bound to the same owner
    op_ctx = AuthContext(scope="operator", user_id=owner)
    res2 = ptool.loopskill_placements(db_session, str(fleet.id), ctx=op_ctx)
    assert res2["count"] >= 1


# ── capability / secret preflight ────────────────────────────────────────────


def test_assign_refused_on_missing_secret(db_session):
    owner = uuid4()
    fleet = _mk_fleet(db_session, owner_id=owner)
    m1 = _mk_member(db_session, fleet, "a")
    # member advertises NO secrets
    _advertise(db_session, m1, os="linux")
    _mk_manifest(db_session, "loop-x", secret_refs=[{"name": "FAKE_SECRET", "required": True}])
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=owner)
    res = ptool.loopskill_assign(db_session, str(fleet.id), "loop-x", str(m1.id), ctx=ctx)
    assert res["code"] == 409
    assert any("FAKE_SECRET" in x for x in res.get("missing", []))


def test_assign_succeeds_when_secret_advertised(db_session):
    owner = uuid4()
    fleet = _mk_fleet(db_session, owner_id=owner)
    m1 = _mk_member(db_session, fleet, "a")
    _advertise(db_session, m1, os="linux", secrets=["REAL_SECRET"])
    _mk_manifest(db_session, "loop-x", secret_refs=[{"name": "REAL_SECRET", "required": True}])
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=owner)
    res = ptool.loopskill_assign(db_session, str(fleet.id), "loop-x", str(m1.id), ctx=ctx)
    assert res["assigned"] is True


# ── stale-member alert ───────────────────────────────────────────────────────


def test_stale_member_alert_fires(db_session):
    fleet = _mk_fleet(db_session)
    m1 = _mk_member(db_session, fleet, "fresh")
    m2 = _mk_member(db_session, fleet, "stale")
    now = datetime.now(timezone.utc)
    # fresh: pinged 1 min ago
    lv1 = _advertise(db_session, m1)
    lv1.last_ping_at = now - timedelta(minutes=1)
    # stale: pinged 30 min ago, interval 300s → threshold 900s (15 min)
    lv2 = _advertise(db_session, m2)
    lv2.last_ping_at = now - timedelta(minutes=30)
    db_session.commit()

    stale = find_stale_members(db_session, now=now)
    stale_hosts = {s.host for s in stale}
    assert "stale" in stale_hosts
    assert "fresh" not in stale_hosts


def test_never_pinged_member_is_stale(db_session):
    fleet = _mk_fleet(db_session)
    _mk_member(db_session, fleet, "silent")  # no liveness row
    db_session.commit()
    stale = find_stale_members(db_session)
    assert any(s.host == "silent" for s in stale)


# ── MCP server dispatch (tools are reachable through the real server) ─────────


def test_placement_tools_dispatch_through_server(db_session):
    """The placement tools are wired into the real MCP dispatch chain, not just
    callable in isolation — the analogue of the 'route exists in create_app' guard."""
    from app.mcp.server import _dispatch

    owner = uuid4()
    fleet = _mk_fleet(db_session, owner_id=owner)
    m1 = _mk_member(db_session, fleet, "a")
    _advertise(db_session, m1, os="linux")
    db_session.commit()

    caller = {"scope": "user", "user_id": owner}
    # assign via the server dispatch
    res = _dispatch(
        "loopskill_assign",
        db_session,
        {
            "fleet_id": str(fleet.id),
            "loop_key": "loop-x",
            "member_id": str(m1.id),
        },
        caller,
    )
    assert res["assigned"] is True
    # placements read via the server dispatch
    res2 = _dispatch("loopskill_placements", db_session, {"fleet_id": str(fleet.id)}, caller)
    assert res2["count"] == 1
    assert res2["placements"][0]["loop_key"] == "loop-x"


def test_placement_tool_member_key_403_through_server(db_session):
    """A bare fleet-member caller is 403 on a manager tool via the real dispatch."""
    from app.mcp.server import _dispatch

    fleet = _mk_fleet(db_session)
    _mk_member(db_session, fleet, "a")
    db_session.commit()
    caller = {"scope": "fleet", "fleet_id": fleet.id}
    res = _dispatch("loopskill_placements", db_session, {"fleet_id": str(fleet.id)}, caller)
    assert res["code"] == 403


# ── reconcile pre-apply gate (fleetos_1607 gap-close, 2026-08-07) ────────────


def test_reconcile_precheck_clean_fleet_ok(db_session):
    """No live placements → ok=True, checked=0, no false-positive drift."""
    owner = uuid4()
    fleet = _mk_fleet(db_session, owner_id=owner)
    db_session.commit()
    ctx = AuthContext(scope="user", user_id=owner)
    res = ptool.loopskill_reconcile_precheck(db_session, str(fleet.id), ctx=ctx)
    assert res["ok"] is True
    assert res["checked"] == 0
    assert res["incompatible"] == []


def test_reconcile_precheck_detects_post_assign_drift(db_session):
    """The keystone case: a placement was FINE at assign time, then the
    manifest was re-declared with a new requirement the member can't satisfy.
    Precheck must surface it — nothing else re-runs preflight after assign."""
    owner = uuid4()
    fleet = _mk_fleet(db_session, owner_id=owner)
    m1 = _mk_member(db_session, fleet, "a")
    _advertise(db_session, m1, os="linux")
    _mk_manifest(db_session, "loop-x", requires={})
    db_session.commit()

    # assign succeeds — member satisfies the (empty) requirements at this point
    p = psvc.assign(db_session, fleet.id, "loop-x", m1.id)
    assert p.status == "active"

    # manifest drifts: now requires a package the member never advertised
    manifest = db_session.query(LoopManifest).filter_by(loop_id="loop-x").one()
    manifest.requires = {"packages": ["cuda"]}
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=owner)
    res = ptool.loopskill_reconcile_precheck(db_session, str(fleet.id), ctx=ctx)
    assert res["ok"] is False
    assert res["checked"] == 1
    assert len(res["incompatible"]) == 1
    entry = res["incompatible"][0]
    assert entry["loop_key"] == "loop-x"
    assert any("cuda" in x for x in entry["missing"])


def test_reconcile_precheck_ignores_removed_placements(db_session):
    """A removed (evacuated) placement isn't live — drift on it doesn't count."""
    owner = uuid4()
    fleet = _mk_fleet(db_session, owner_id=owner)
    m1 = _mk_member(db_session, fleet, "a")
    _advertise(db_session, m1, os="linux")
    _mk_manifest(db_session, "loop-x", requires={})
    db_session.commit()

    psvc.assign(db_session, fleet.id, "loop-x", m1.id)
    psvc.evacuate(db_session, fleet.id, "loop-x")

    manifest = db_session.query(LoopManifest).filter_by(loop_id="loop-x").one()
    manifest.requires = {"packages": ["cuda"]}
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=owner)
    res = ptool.loopskill_reconcile_precheck(db_session, str(fleet.id), ctx=ctx)
    assert res["ok"] is True
    assert res["checked"] == 0


def test_reconcile_precheck_member_key_403(db_session):
    """Manager-capability gated, same as the rest of the placement surface."""
    fleet = _mk_fleet(db_session)
    m1 = _mk_member(db_session, fleet, "a")
    _advertise(db_session, m1, os="linux")
    db_session.commit()
    member_ctx = AuthContext(scope="fleet", fleet_id=fleet.id)
    res = ptool.loopskill_reconcile_precheck(db_session, str(fleet.id), ctx=member_ctx)
    assert res["code"] == 403


def test_reconcile_precheck_dispatches_through_server(db_session):
    """Reachable through the real MCP dispatch chain, not just callable in isolation."""
    from app.mcp.server import _dispatch

    owner = uuid4()
    fleet = _mk_fleet(db_session, owner_id=owner)
    m1 = _mk_member(db_session, fleet, "a")
    _advertise(db_session, m1, os="linux")
    _mk_manifest(db_session, "loop-x", requires={})
    db_session.commit()
    psvc.assign(db_session, fleet.id, "loop-x", m1.id)

    caller = {"scope": "user", "user_id": owner}
    res = _dispatch("loopskill_reconcile_precheck", db_session, {"fleet_id": str(fleet.id)}, caller)
    assert res["ok"] is True
    assert res["checked"] == 1
