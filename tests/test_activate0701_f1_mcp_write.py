"""Phase F1 (loopskill_activate_0701) — MCP write-surface tests.

Gate: Tori runs declare→deploy→observe→hear-voice entirely via MCP dispatch.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests._app_factory import build_test_app


@pytest.fixture
def client(db_session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _setup(db):
    from app.api_key_routes import _generate_key
    from app.models import APIKey, Bundle, Fleet, User
    import hashlib

    u = User(email="t@t.com", display_name="T", subscription_tier="pro")
    db.add(u)
    db.flush()
    pt, pfx, hs = _generate_key()
    k = APIKey(user_id=u.id, key_prefix=pfx, key_hash=hs, is_active=True)
    db.add(k)
    db.flush()
    f = Fleet(owner_user_id=u.id, name="f", fleet_api_key_hash=hashlib.sha256(b"x").hexdigest())
    db.add(f)
    db.flush()
    b = Bundle(name="b", bundle_owner=u.id)
    db.add(b)
    db.flush()
    db.commit()
    return u, pt, k, f, b


def test_bundle_deploy_via_dispatch(client, db_session):
    from app.auth_ctx import AuthContext
    from app.mcp.tools.fleet_write import dispatch_f1, _NOT_HANDLED

    u, pt, k, f, b = _setup(db_session)
    result = dispatch_f1(
        "loopskill_bundle_deploy",
        db_session,
        {
            "bundle_id": str(b.id),
            "skills": [],
            "connectors": [],
            "composite_loops": [],
            "personalities": [],
        },
        ctx=AuthContext(scope="master"),
    )
    assert result is not _NOT_HANDLED
    assert result["deployed"] is True


def test_reconcile_status_via_dispatch(client, db_session):
    from app.auth_ctx import AuthContext
    from app.mcp.tools.fleet_write import dispatch_f1, _NOT_HANDLED

    u, pt, k, f, b = _setup(db_session)
    result = dispatch_f1(
        "loopskill_reconcile_status",
        db_session,
        {
            "fleet_id": str(f.id),
        },
        ctx=AuthContext(scope="master"),
    )
    assert result is not _NOT_HANDLED
    assert "members" in result
    assert isinstance(result["members"], list)


def test_voice_inbox_read_via_dispatch(client, db_session):
    from app.auth_ctx import AuthContext
    from app.mcp.tools.fleet_write import dispatch_f1, _NOT_HANDLED

    u, pt, k, f, b = _setup(db_session)
    result = dispatch_f1(
        "loopskill_voice_inbox_read",
        db_session,
        {
            "fleet_id": str(f.id),
            "limit": 10,
        },
        ctx=AuthContext(scope="master"),
    )
    assert result is not _NOT_HANDLED
    assert "items" in result


def test_unknown_tool_returns_not_handled(db_session):
    from app.auth_ctx import AuthContext
    from app.mcp.tools.fleet_write import dispatch_f1, _NOT_HANDLED

    result = dispatch_f1("nonexistent_tool", db_session, {}, ctx=AuthContext(scope="master"))
    assert result is _NOT_HANDLED
