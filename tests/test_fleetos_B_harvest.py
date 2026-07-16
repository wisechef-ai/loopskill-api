"""tests/test_fleetos_B_harvest.py — fleetos_1607 Phase B gate suite.

RED-proofs harvest (reverse GitOps via the shipped feedback rail):
  * a hand-created cron harvested → proposed as exactly one new-local diff.
  * a poisoned member (embedded credential / crafted path) is BLOCKED, never
    proposed.
  * modified-local is detected; unchanged loops are not proposed; missing-local
    (bundle has it, agent dropped it) is surfaced.
  * signature binding — a wrong-signature report is rejected.
  * routing: with a configured feedback_repo the proposal goes through the rail
    (dispatch_issue), else it falls back to the in-app feed.
  * ZERO new auth code — the proposal rides the existing feedback_repo/PAT vault.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from uuid import uuid4

from app.auth_ctx import AuthContext
from app.models import APIKey, Bundle, FleetMember, LoopManifest, User
from app.services import harvest as hsvc
from app.mcp.tools import harvest as htool


# ── fixtures ─────────────────────────────────────────────────────────────────


def _mk_user(db):
    u = User(id=uuid4(), display_name="owner")
    db.add(u)
    db.flush()
    return u


def _mk_bundle(db, owner, feedback_repo=None, feedback_pat_enc=None):
    b = Bundle(
        id=uuid4(),
        name="golden",
        bundle_owner=owner.id,
        feedback_repo=feedback_repo,
        feedback_mode="pat" if feedback_repo else None,
        feedback_pat_enc=feedback_pat_enc,
    )
    db.add(b)
    db.flush()
    return b


def _mk_manifest(db, owner, loop_key, prompt="do the thing", schedule="0 9 * * *"):
    m = LoopManifest(
        id=uuid4(),
        loop_id=loop_key,
        owner_user_id=owner.id,
        schedule=schedule,
        prompt=prompt,
        skills=[],
        requires={},
        secret_refs=[],
        reserved={},
    )
    db.add(m)
    db.flush()
    return m


def _mk_member(db, owner):
    key = APIKey(
        id=uuid4(), user_id=owner.id, key_prefix=uuid4().hex[:8], key_hash=f"h-{uuid4().hex}", name="mk"
    )
    db.add(key)
    db.flush()
    # a bare Fleet is needed for the FK
    from app.models import Fleet

    fleet = Fleet(id=uuid4(), owner_user_id=owner.id, name="f", fleet_api_key_hash=f"fh-{uuid4().hex}")
    db.add(fleet)
    db.flush()
    m = FleetMember(
        id=uuid4(),
        fleet_id=fleet.id,
        host="a",
        profile="default",
        skills_dir="~/.hermes/loopskill",
        api_key_id=key.id,
    )
    db.add(m)
    db.flush()
    return m, key


def _loop(loop_id, prompt="do the thing", schedule="0 9 * * *", **kw):
    return {"loop_id": loop_id, "schedule": schedule, "prompt": prompt, **kw}


# ── diff engine ──────────────────────────────────────────────────────────────


def test_new_local_loop_proposed(db_session):
    owner = _mk_user(db_session)
    bundle = _mk_bundle(db_session, owner)
    _mk_manifest(db_session, owner, "existing-loop")
    db_session.commit()

    # agent harvested the existing loop PLUS a new hand-created one
    harvested = [
        _loop("existing-loop"),
        _loop("brand-new-cron", prompt="new daily task", schedule="30m"),
    ]
    result = hsvc.diff_harvest(db_session, "member-1", bundle, harvested)
    new = [d for d in result.diffs if d.verdict == hsvc.NEW_LOCAL]
    assert [d.loop_key for d in new] == ["brand-new-cron"]
    # the existing one is unchanged, not proposed
    assert any(d.verdict == hsvc.UNCHANGED and d.loop_key == "existing-loop" for d in result.diffs)
    assert len(result.proposable) == 1


def test_modified_local_detected(db_session):
    owner = _mk_user(db_session)
    bundle = _mk_bundle(db_session, owner)
    _mk_manifest(db_session, owner, "loop-x", prompt="original prompt")
    db_session.commit()
    harvested = [_loop("loop-x", prompt="MUTATED prompt")]
    result = hsvc.diff_harvest(db_session, "m", bundle, harvested)
    mod = [d for d in result.diffs if d.verdict == hsvc.MODIFIED_LOCAL]
    assert [d.loop_key for d in mod] == ["loop-x"]
    assert mod[0].provenance == hsvc.PROV_MUTATED


def test_missing_local_surfaced(db_session):
    owner = _mk_user(db_session)
    bundle = _mk_bundle(db_session, owner)
    _mk_manifest(db_session, owner, "loop-a")
    _mk_manifest(db_session, owner, "loop-b")
    db_session.commit()
    # agent only harvested loop-a → loop-b is missing-local
    result = hsvc.diff_harvest(db_session, "m", bundle, [_loop("loop-a")])
    miss = [d for d in result.diffs if d.verdict == hsvc.MISSING_LOCAL]
    assert [d.loop_key for d in miss] == ["loop-b"]


# ── security gate (RED-proof) ────────────────────────────────────────────────


def test_poisoned_loop_blocked_secret(db_session):
    owner = _mk_user(db_session)
    bundle = _mk_bundle(db_session, owner)
    db_session.commit()
    # embedded credential in the harvested prompt
    poison = _loop("evil-loop", prompt="export TOKEN=" + "ghp_" + ("a" * 36) + " && run")
    result = hsvc.diff_harvest(db_session, "m", bundle, [poison])
    assert result.blocked
    assert result.blocked[0]["loop_key"] == "evil-loop"
    # blocked loop is NOT in the proposable set
    assert not any(d.loop_key == "evil-loop" for d in result.proposable)


def test_poisoned_loop_blocked_path_escape(db_session):
    owner = _mk_user(db_session)
    bundle = _mk_bundle(db_session, owner)
    db_session.commit()
    poison = _loop("evil-loop", skills=[{"id": "../../etc/passwd", "hash": "sha256:x"}])
    result = hsvc.diff_harvest(db_session, "m", bundle, [poison])
    assert result.blocked
    assert "path-escape" in result.blocked[0]["findings"][0]


# ── signature binding ────────────────────────────────────────────────────────


def test_signature_verify():
    key_hash = "member-key-hash-abc"
    payload = json.dumps([{"loop_id": "x"}], sort_keys=True, separators=(",", ":"))
    good = hmac.new(key_hash.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert hsvc.verify_harvest_signature(payload, key_hash, good) is True
    assert hsvc.verify_harvest_signature(payload, key_hash, "deadbeef") is False


def test_harvest_tool_rejects_bad_signature(db_session):
    owner = _mk_user(db_session)
    bundle = _mk_bundle(db_session, owner)
    member, key = _mk_member(db_session, owner)
    db_session.commit()
    ctx = AuthContext(scope="user", user_id=owner.id)
    res = htool.loopskill_harvest(
        db_session,
        str(bundle.id),
        str(member.id),
        harvested_loops=[_loop("x")],
        signature="wrong",
        ctx=ctx,
    )
    assert res["code"] == 401


# ── routing (in-app fallback; no external network in tests) ───────────────────


def test_harvest_routes_to_in_app_feed_when_no_repo(db_session):
    owner = _mk_user(db_session)
    bundle = _mk_bundle(db_session, owner)  # no feedback_repo configured
    member, key = _mk_member(db_session, owner)
    db_session.commit()
    ctx = AuthContext(scope="user", user_id=owner.id)
    # sign correctly
    loops = [_loop("new-loop")]
    payload = json.dumps(loops, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(key.key_hash.encode(), payload.encode(), hashlib.sha256).hexdigest()
    res = htool.loopskill_harvest(
        db_session,
        str(bundle.id),
        str(member.id),
        harvested_loops=loops,
        signature=sig,
        ctx=ctx,
    )
    assert res["drift"] is True
    assert res["routed"] == "in_app_feed"
    assert res["summary"]["new_local"] == 1


def test_harvest_no_drift_returns_clean(db_session):
    owner = _mk_user(db_session)
    bundle = _mk_bundle(db_session, owner)
    _mk_manifest(db_session, owner, "loop-x")
    member, key = _mk_member(db_session, owner)
    db_session.commit()
    ctx = AuthContext(scope="user", user_id=owner.id)
    res = htool.loopskill_harvest(
        db_session,
        str(bundle.id),
        str(member.id),
        harvested_loops=[_loop("loop-x")],
        ctx=ctx,
    )
    assert res["drift"] is False


def test_harvest_forbidden_for_non_owner(db_session):
    owner = _mk_user(db_session)
    other = _mk_user(db_session)
    bundle = _mk_bundle(db_session, owner)
    member, key = _mk_member(db_session, owner)
    db_session.commit()
    ctx = AuthContext(scope="user", user_id=other.id)
    res = htool.loopskill_harvest(
        db_session,
        str(bundle.id),
        str(member.id),
        harvested_loops=[_loop("x")],
        ctx=ctx,
    )
    assert res["code"] == 403


# ── end-to-end through the real MCP server dispatch ──────────────────────────


def test_harvest_dispatches_through_server(db_session):
    from app.mcp.server import _dispatch

    owner = _mk_user(db_session)
    bundle = _mk_bundle(db_session, owner)
    member, key = _mk_member(db_session, owner)
    db_session.commit()
    caller = {"scope": "user", "user_id": owner.id}
    res = _dispatch(
        "loopskill_harvest",
        db_session,
        {
            "bundle_id": str(bundle.id),
            "member_id": str(member.id),
            "harvested_loops": [_loop("srv-loop")],
        },
        caller,
    )
    assert res["drift"] is True
