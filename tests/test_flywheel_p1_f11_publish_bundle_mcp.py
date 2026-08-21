"""Tests for flywheel Phase-1 F1.1 — loopskill_publish_bundle MCP tool.

Covers: happy path, idempotency (already-public), non-owner (403-equivalent
== 404, no existence oracle, mirrors loopskill_harvest), anonymous fail-closed,
tool registration in the MCP registry/healthz surface, and the compose tool's
follow-up message pointing at the real MCP verb.
"""

from __future__ import annotations

from uuid import uuid4

from app.auth_ctx import AuthContext
from app.mcp.server import call_tool_sync
from app.mcp.registry import _tool_definitions
from app.mcp.tools.bundle_publish import loopskill_publish_bundle
from app.models import Bundle, User

_ATTACKER_CALLER = {
    "scope": "user",
    "user_id": uuid4(),
    "api_key_id": None,
    "auth_ctx": AuthContext(scope="user", user_id=uuid4()),
}


def _mk_user(db, email: str | None = None) -> User:
    email = email or f"u-{uuid4().hex[:8]}@example.com"
    user = User(id=uuid4(), display_name=email, email=email, subscription_tier="free")
    db.add(user)
    db.flush()
    return user


def _mk_bundle(db, owner, *, visibility="private", name="My Stack", slug=None) -> Bundle:
    cb = Bundle(id=uuid4(), name=name, bundle_owner=owner.id, visibility=visibility, slug=slug)
    db.add(cb)
    db.commit()
    db.refresh(cb)
    return cb


def _caller_for(user: User) -> dict:
    return {
        "scope": "user",
        "user_id": user.id,
        "api_key_id": None,
        "auth_ctx": AuthContext(scope="user", user_id=user.id),
    }


class TestPublishBundleHappyPath:
    def test_private_bundle_publishes_and_mints_slug(self, db_session):
        owner = _mk_user(db_session)
        cb = _mk_bundle(db_session, owner, visibility="private", slug=None)
        assert cb.slug is None

        out = call_tool_sync(
            "loopskill_publish_bundle",
            {"bundle_id": str(cb.id)},
            caller=_caller_for(owner),
            db=db_session,
        )

        assert out["published"] is True
        assert out["visibility"] == "public"
        assert out["was_public"] is False
        assert out["transition"] == "private_to_public"
        assert out["slug"], f"expected a minted slug; got {out!r}"
        assert out["bundle_url"] == f"bundle://{out['slug']}"

        db_session.refresh(cb)
        assert cb.visibility == "public"
        assert cb.slug is not None

    def test_master_scope_can_publish_any_bundle(self, db_session):
        owner = _mk_user(db_session)
        cb = _mk_bundle(db_session, owner, visibility="private")
        master_caller = {
            "scope": "master",
            "user_id": None,
            "api_key_id": None,
            "auth_ctx": AuthContext(scope="master"),
        }
        out = call_tool_sync(
            "loopskill_publish_bundle", {"bundle_id": str(cb.id)}, caller=master_caller, db=db_session
        )
        assert out["published"] is True
        assert out["visibility"] == "public"


class TestPublishBundleIdempotency:
    def test_already_public_returns_success_not_error(self, db_session):
        owner = _mk_user(db_session)
        cb = _mk_bundle(db_session, owner, visibility="public", slug=f"already-public-{uuid4().hex[:8]}")
        original_slug = cb.slug

        out = call_tool_sync(
            "loopskill_publish_bundle",
            {"bundle_id": str(cb.id)},
            caller=_caller_for(owner),
            db=db_session,
        )

        assert out["published"] is True
        assert out["was_public"] is True
        assert out["transition"] == "already_public"
        assert out["visibility"] == "public"
        # Idempotent: slug is never re-minted / changed on a no-op call.
        assert out["slug"] == original_slug
        assert "error" not in out

    def test_double_publish_is_a_true_no_op_on_second_call(self, db_session):
        owner = _mk_user(db_session)
        cb = _mk_bundle(db_session, owner, visibility="private")

        first = call_tool_sync(
            "loopskill_publish_bundle",
            {"bundle_id": str(cb.id)},
            caller=_caller_for(owner),
            db=db_session,
        )
        second = call_tool_sync(
            "loopskill_publish_bundle",
            {"bundle_id": str(cb.id)},
            caller=_caller_for(owner),
            db=db_session,
        )

        assert first["transition"] == "private_to_public"
        assert second["transition"] == "already_public"
        assert first["slug"] == second["slug"]


class TestPublishBundleNonOwner:
    def test_non_owner_gets_bundle_not_found_not_403(self, db_session):
        owner = _mk_user(db_session, "owner-nonowner@example.com")
        cb = _mk_bundle(db_session, owner, visibility="private")

        out = call_tool_sync(
            "loopskill_publish_bundle",
            {"bundle_id": str(cb.id)},
            caller=_ATTACKER_CALLER,
            db=db_session,
        )
        # mesh_0408 W1b precedent: no 403-vs-404 oracle — the non-owner answer
        # is byte-identical to the nonexistent-bundle answer.
        assert out == {"error": "bundle_not_found", "status": 404}

        db_session.refresh(cb)
        assert cb.visibility == "private", "EXPLOIT: non-owner flipped a bundle they don't own to public"

    def test_nonexistent_bundle_id_same_answer_as_non_owner(self, db_session):
        owner = _mk_user(db_session)
        _caller = _caller_for(owner)
        out = call_tool_sync(
            "loopskill_publish_bundle",
            {"bundle_id": str(uuid4())},
            caller=_caller,
            db=db_session,
        )
        assert out == {"error": "bundle_not_found", "status": 404}


class TestPublishBundleAnonymousFailsClosed:
    def test_anonymous_caller_401s_and_does_not_publish(self, db_session):
        owner = _mk_user(db_session)
        cb = _mk_bundle(db_session, owner, visibility="private")
        anon_caller = {
            "scope": "anonymous",
            "user_id": None,
            "api_key_id": None,
            "auth_ctx": AuthContext.anonymous(),
        }

        out = call_tool_sync(
            "loopskill_publish_bundle",
            {"bundle_id": str(cb.id)},
            caller=anon_caller,
            db=db_session,
        )
        assert out == {"error": "auth_required", "status": 401}

        db_session.refresh(cb)
        assert cb.visibility == "private", "EXPLOIT: anonymous caller published a bundle"

    def test_direct_call_with_none_ctx_fails_closed(self, db_session):
        owner = _mk_user(db_session)
        cb = _mk_bundle(db_session, owner, visibility="private")
        out = loopskill_publish_bundle(db_session, bundle_id=str(cb.id), ctx=None)
        assert out == {"error": "auth_required", "status": 401}


class TestPublishBundleRegistration:
    def test_tool_is_registered_in_mcp_definitions(self):
        names = {t.name for t in _tool_definitions()}
        assert "loopskill_publish_bundle" in names

    def test_tool_appears_in_mcp_healthz(self):
        from app.mcp.server import mcp_healthz

        body = mcp_healthz()
        assert "loopskill_publish_bundle" in body["tools"]


class TestComposeFollowUpPointsAtRealVerb:
    def test_compose_next_message_names_loopskill_publish_bundle(self, db_session):
        from app.mcp.tools.bundle_stream import loopskill_compose_bundle_from_links
        from app.models import Skill

        owner = _mk_user(db_session)
        skill = Skill(id=uuid4(), slug=f"compose-skill-{uuid4().hex[:8]}", title="S", is_public=True)
        db_session.add(skill)
        db_session.commit()

        out = loopskill_compose_bundle_from_links(
            db_session, links=[f"skill://{skill.slug}"], ctx=_caller_for(owner)["auth_ctx"]
        )
        assert "loopskill_publish_bundle" in out["next"], (
            f"compose follow-up must point at the real MCP publish verb, not a REST dead-end; got {out['next']!r}"
        )
        assert "PATCH" not in out["next"]
        assert out["cookbook"] in out["next"]
