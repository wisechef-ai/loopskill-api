"""P0 (converge_0208) — the cross-tenant MCP bundle-read leak.

``app/mcp/tools/list_cookbook.py::loopskill_list_bundle`` resolved a bundle by
raw ``cookbook_id`` without ever consulting the caller's identity — any
authenticated user could read ANY other user's bundle (owner id, name, full
skill list including pinned versions) over ``POST /api/mcp/http``.

RED tests below drive the tool through the real MCP dispatch path
(``call_tool_sync`` -> ``_dispatch``) rather than the bare function, because
the vulnerability is in how the dispatcher (fails to) thread caller identity
into the tool — calling the bare function directly would not exercise that
gap.
"""

from __future__ import annotations

from uuid import uuid4

from app.auth_ctx import AuthContext
from app.mcp.server import call_tool_sync
from app.models import Bundle, BundleSkill, User
from tests.conftest import make_skill


def _make_user(db, email: str) -> User:
    user = User(
        id=uuid4(),
        display_name=email,
        email=email,
        subscription_tier="pro",
        subscription_status="active",
    )
    db.add(user)
    db.flush()
    return user


def _caller_for(user_id) -> dict:
    """Build a caller dict the way the real SSE/HTTP transport does —
    with a real AuthContext under 'auth_ctx' (see app/mcp/auth.py::validate_key).
    """
    ctx = AuthContext(scope="user", user_id=user_id)
    return {"scope": "user", "user_id": user_id, "api_key_id": None, "auth_ctx": ctx}


class TestCrossTenantBundleReadViaMcpListBundle:
    """CVE-shape: cross-tenant bundle read via MCP list_bundle."""

    def test_red_tenant_a_cannot_read_tenant_b_bundle_by_id(self, db_session):
        user_a = _make_user(db_session, "alice-p0@example.com")
        user_b = _make_user(db_session, "bob-p0@example.com")
        bundle_b = Bundle(id=uuid4(), name="Bob's Private Bundle", bundle_owner=user_b.id)
        db_session.add(bundle_b)
        skill = make_skill(db_session, slug="bob-private-skill", title="Bob Private Skill")
        db_session.add(
            BundleSkill(
                bundle_id=bundle_b.id,
                skill_id=skill.id,
                source="custom-added",
                pinned_version="3.2.1",
            )
        )
        db_session.commit()

        # Attacker: user A, calling with user B's bundle id, through the REAL
        # dispatch path (call_tool_sync -> _dispatch), not the bare function.
        out = call_tool_sync(
            "loopskill_list_bundle",
            {"cookbook_id": str(bundle_b.id)},
            caller=_caller_for(user_a.id),
            db=db_session,
        )

        leaked_owner = (out.get("cookbook") or {}).get("owner")
        assert leaked_owner != str(user_b.id), (
            f"CROSS-TENANT LEAK: user A read user B's bundle via MCP list_bundle; "
            f"got {out!r}"
        )
        assert out.get("cookbook") is None, (
            f"CROSS-TENANT LEAK: unauthorized bundle read returned data instead of "
            f"a not-found/forbidden result; got {out!r}"
        )
        skills = out.get("skills") or []
        assert not any(s.get("slug") == "bob-private-skill" for s in skills), (
            f"CROSS-TENANT LEAK: Bob's private skill list leaked to Alice; got {out!r}"
        )

    def test_red_owner_can_still_read_own_bundle_by_id(self, db_session):
        """Sanity: the legitimate owner must still be able to read their own
        bundle by id through the same dispatch path (no regression)."""
        owner = _make_user(db_session, "owner-p0@example.com")
        bundle = Bundle(id=uuid4(), name="Owner's Bundle", bundle_owner=owner.id)
        db_session.add(bundle)
        db_session.commit()

        out = call_tool_sync(
            "loopskill_list_bundle",
            {"cookbook_id": str(bundle.id)},
            caller=_caller_for(owner.id),
            db=db_session,
        )
        assert out.get("cookbook") is not None, f"Owner should read their own bundle; got {out!r}"
        assert out["cookbook"]["id"] == str(bundle.id)

    def test_red_master_can_read_any_bundle(self, db_session):
        """Sanity: master scope retains full read access."""
        owner = _make_user(db_session, "owner2-p0@example.com")
        bundle = Bundle(id=uuid4(), name="Someone's Bundle", bundle_owner=owner.id)
        db_session.add(bundle)
        db_session.commit()

        master_caller = {
            "scope": "master",
            "user_id": None,
            "api_key_id": None,
            "auth_ctx": AuthContext(scope="master"),
        }
        out = call_tool_sync(
            "loopskill_list_bundle",
            {"cookbook_id": str(bundle.id)},
            caller=master_caller,
            db=db_session,
        )
        assert out.get("cookbook") is not None, f"Master should read any bundle; got {out!r}"
        assert out["cookbook"]["id"] == str(bundle.id)
