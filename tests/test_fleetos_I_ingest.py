"""tests/test_fleetos_I_ingest.py — fleetos_1607 Phase I gate suite.

RED-proofs the ingest surface (the write path that makes the placement chain
operable):
  * loopskill_ping upserts a FleetMemberLiveness row with typed provides{}.
  * loopskill_declare_loop upserts a LoopManifest (create then version-bump).
  * both are owner-gated (non-owner -> 403).
  * THE KEYSTONE: declare_loop + ping make loopskill_assign SUCCEED — the exact
    chain that was inert (no manifest, no liveness) before this phase.
"""

from __future__ import annotations

from uuid import uuid4

from app.auth_ctx import AuthContext
from app.models import APIKey, Fleet, FleetMember, FleetMemberLiveness, LoopManifest, User
from app.mcp.tools import fleet_ingest as ing
from app.services import placement as placement_svc


def _mk_fleet(db, owner_id=None):
    owner_id = owner_id or uuid4()
    f = Fleet(id=uuid4(), owner_user_id=owner_id, name="f", fleet_api_key_hash=f"fh-{uuid4().hex}")
    db.add(f)
    db.flush()
    return f


def _mk_member(db, fleet, host="a"):
    u = User(id=uuid4(), display_name=host)
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


def _owner_ctx(fleet):
    return AuthContext(scope="user", user_id=fleet.owner_user_id)


# ── ping ─────────────────────────────────────────────────────────────────────


def test_ping_creates_liveness(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    res = ing.loopskill_ping(
        db_session,
        str(m.id),
        provides={"os": "linux", "arch": "x86_64", "runtimes": {"python": "3.12.0"}},
        ctx=_owner_ctx(fleet),
    )
    assert res["ok"] is True
    row = db_session.query(FleetMemberLiveness).filter_by(member_id=m.id).one()
    assert row.provides["os"] == "linux"


def test_ping_is_idempotent_upsert(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    ing.loopskill_ping(db_session, str(m.id), provides={"os": "linux"}, ctx=_owner_ctx(fleet))
    ing.loopskill_ping(
        db_session, str(m.id), provides={"os": "linux", "arch": "arm64"}, ctx=_owner_ctx(fleet)
    )
    rows = db_session.query(FleetMemberLiveness).filter_by(member_id=m.id).all()
    assert len(rows) == 1  # upsert, not duplicate
    assert rows[0].provides["arch"] == "arm64"


def test_ping_non_owner_forbidden(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    res = ing.loopskill_ping(
        db_session, str(m.id), provides={"os": "linux"}, ctx=AuthContext(scope="user", user_id=uuid4())
    )
    assert res["code"] == 403


# ── declare_loop ─────────────────────────────────────────────────────────────


def test_declare_loop_creates_manifest(db_session):
    fleet = _mk_fleet(db_session)
    db_session.commit()
    res = ing.loopskill_declare_loop(
        db_session,
        str(fleet.id),
        "daily-digest",
        "0 9 * * *",
        "summarize the day",
        requires={"os": ["linux"]},
        safety_class="idempotent",
        ctx=_owner_ctx(fleet),
    )
    assert res["created"] is True
    assert res["manifest_version"] == 1
    m = db_session.query(LoopManifest).filter_by(loop_id="daily-digest").one()
    assert m.safety_class == "idempotent"
    assert m.requires == {"os": ["linux"]}


def test_declare_loop_upsert_bumps_version(db_session):
    fleet = _mk_fleet(db_session)
    db_session.commit()
    ing.loopskill_declare_loop(db_session, str(fleet.id), "loop-x", "30m", "v1", ctx=_owner_ctx(fleet))
    res = ing.loopskill_declare_loop(
        db_session, str(fleet.id), "loop-x", "30m", "v2 prompt", ctx=_owner_ctx(fleet)
    )
    assert res["created"] is False
    assert res["manifest_version"] == 2
    assert db_session.query(LoopManifest).filter_by(loop_id="loop-x").count() == 1


def test_declare_loop_validates_safety_class(db_session):
    fleet = _mk_fleet(db_session)
    db_session.commit()
    res = ing.loopskill_declare_loop(
        db_session, str(fleet.id), "x", "30m", "p", safety_class="whatever", ctx=_owner_ctx(fleet)
    )
    assert res["code"] == 422


def test_declare_loop_non_owner_forbidden(db_session):
    fleet = _mk_fleet(db_session)
    db_session.commit()
    res = ing.loopskill_declare_loop(
        db_session, str(fleet.id), "x", "30m", "p", ctx=AuthContext(scope="user", user_id=uuid4())
    )
    assert res["code"] == 403


# ── THE KEYSTONE: ingest makes the placement chain operable ──────────────────


def test_ingest_makes_assign_succeed(db_session):
    """Before Phase I: no manifest + no liveness => assign always failed preflight.
    After: declare_loop + ping => assign SUCCEEDS. This is the live-move gate."""
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet, "adam-xps")
    db_session.commit()
    octx = _owner_ctx(fleet)

    # 1. declare the loop's desired state
    ing.loopskill_declare_loop(
        db_session,
        str(fleet.id),
        "supervision-loop",
        "30m",
        "supervise the fleet",
        requires={"os": ["linux"]},
        ctx=octx,
    )
    # 2. member advertises it can host it
    ing.loopskill_ping(
        db_session,
        str(m.id),
        provides={"os": "linux", "arch": "x86_64"},
        ctx=octx,
    )
    # 3. preflight now PASSES (was: member-never-pinged / no requires)
    pf = placement_svc.preflight_member(db_session, m.id, "supervision-loop")
    assert pf.ok is True, f"preflight should pass, missing={pf.missing}"

    # 4. assign SUCCEEDS — a real placement row is created at epoch 1
    pl = placement_svc.assign(db_session, fleet.id, "supervision-loop", m.id, op_id="op-1")
    assert pl.placement_epoch == 1
    assert pl.status in ("assigned", "active")


def test_assign_still_refuses_uncapable_member(db_session):
    """A member that pinged but lacks a required capability still fails — the
    ingest path doesn't weaken preflight."""
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet, "mac01")
    db_session.commit()
    octx = _owner_ctx(fleet)
    ing.loopskill_declare_loop(
        db_session,
        str(fleet.id),
        "cuda-loop",
        "30m",
        "train",
        requires={"packages": ["cuda"]},
        ctx=octx,
    )
    ing.loopskill_ping(db_session, str(m.id), provides={"os": "linux", "packages": ["git"]}, ctx=octx)
    pf = placement_svc.preflight_member(db_session, m.id, "cuda-loop")
    assert pf.ok is False
    assert any("cuda" in x for x in pf.missing)
