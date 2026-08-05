"""PARENT edge-case probe the child did NOT cover: master scope + org_id.

master AuthContext has user_id=None, so the B' membership filter becomes
user_id IS NULL. OrgMembership.user_id is NOT NULL, so no row can match and
the call must fail CLOSED (forbidden) rather than crash or create a fleet.
"""
from uuid import uuid4
from app.auth_ctx import AuthContext
from app.mcp.tools.fleet import loopskill_fleet_create


def test_master_scope_with_org_id_fails_closed(db_session):
    ctx = AuthContext(scope="master")
    assert ctx.user_id is None and ctx.org_id is None
    before = db_session.execute.__self__ is not None
    r = loopskill_fleet_create(db_session, name="parent-probe", ctx=ctx, org_id=str(uuid4()))
    assert r.get("error") == "forbidden", f"expected fail-closed, got {r}"


def test_master_without_org_id_hits_preexisting_notnull_not_a_new_bug(db_session):
    """Master + NO org_id must reach the SAME pre-existing NOT NULL failure as
    before B' — proving B' did not change the omitted-org_id path at all.
    (Fleet.owner_user_id is NOT NULL and master has user_id=None; this crash
    predates B' and is out of scope for it.)"""
    import pytest
    from sqlalchemy.exc import IntegrityError
    from app.auth_ctx import AuthContext
    from app.mcp.tools.fleet import loopskill_fleet_create
    ctx = AuthContext(scope="master")
    with pytest.raises(IntegrityError):
        loopskill_fleet_create(db_session, name="parent-probe-2", ctx=ctx)
    db_session.rollback()

