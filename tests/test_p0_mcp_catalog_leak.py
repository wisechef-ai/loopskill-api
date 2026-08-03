"""P0 (converge_0208) — systemic audit follow-up: private-catalog-row leaks.

Auditing every MCP tool in app/mcp/tools/*.py for the same bug class as
list_cookbook.py (a private, user/creator-scoped row returned to any caller
with no ownership check) turned up three more:

  - loopskill_get_loop            (app/mcp/tools/loopskill_catalog.py)
  - loopskill_get_personality     (app/mcp/tools/loopskill_catalog.py)
  - loopskill_get_composite_loop  (app/mcp/tools/composite_loop_catalog.py)

All three only checked ``is_archived`` before returning the FULL private
contract (system_prompt / verification_script / connectors / subagents_config
/ etc.) for ANY slug — including rows with ``is_public=False`` scoped to a
creator via ``creator_id -> Creator.user_id``. Each carries the
``# Public-scope MCP tool:`` marker comment claiming "no private data
exposed", which was false. authz.py already had can_read_personality and
can_read_composite_loop predicates for exactly this ownership chain — they
were just never wired into these tools. A parallel can_read_verifier
predicate is added for the (differently-named) Loop/Verifier model.

RED tests below drive the real MCP dispatch path (call_tool_sync).
"""

from __future__ import annotations

from uuid import uuid4

from app.auth_ctx import AuthContext
from app.mcp.server import call_tool_sync
from app.models import CompositeLoop, Creator, Loop, Personality, User

_ATTACKER_CALLER = {
    "scope": "user",
    "user_id": uuid4(),
    "api_key_id": None,
    "auth_ctx": AuthContext(scope="user", user_id=uuid4()),
}


def _make_creator_user(db, email: str) -> tuple[User, Creator]:
    user = User(id=uuid4(), display_name=email, email=email, subscription_tier="pro")
    db.add(user)
    db.flush()
    creator = Creator(id=uuid4(), user_id=user.id, name=email, slug=f"creator-{uuid4().hex[:8]}")
    db.add(creator)
    db.flush()
    return user, creator


class TestPrivateLoopLeak:
    def test_red_private_loop_secrets_not_leaked_to_stranger(self, db_session):
        _owner, creator = _make_creator_user(db_session, "loop-owner-p0@example.com")
        loop = Loop(
            id=uuid4(),
            slug=f"private-loop-{uuid4().hex[:8]}",
            title="Private Loop",
            is_public=False,
            creator_id=creator.id,
            success_condition="secret success condition",
            verification_script="echo SUPER_SECRET_TOKEN",
            max_turns=10,
            stopping_criteria={},
            tool_allowlist=[],
            system_prompt="SECRET SYSTEM PROMPT",
        )
        db_session.add(loop)
        db_session.commit()

        out = call_tool_sync(
            "loopskill_get_loop", {"slug": loop.slug}, caller=_ATTACKER_CALLER, db=db_session
        )
        assert out.get("system_prompt") is None, (
            f"LEAK: private loop system_prompt exposed to a stranger; got {out!r}"
        )
        assert out.get("verification_script") is None, (
            f"LEAK: private loop verification_script exposed to a stranger; got {out!r}"
        )

    def test_public_loop_still_readable_by_anyone(self, db_session):
        _owner, creator = _make_creator_user(db_session, "loop-owner2-p0@example.com")
        loop = Loop(
            id=uuid4(),
            slug=f"public-loop-{uuid4().hex[:8]}",
            title="Public Loop",
            is_public=True,
            creator_id=creator.id,
            success_condition="ok",
            verification_script="echo ok",
            max_turns=10,
            stopping_criteria={},
            tool_allowlist=[],
            system_prompt="public prompt",
        )
        db_session.add(loop)
        db_session.commit()

        out = call_tool_sync(
            "loopskill_get_loop", {"slug": loop.slug}, caller=_ATTACKER_CALLER, db=db_session
        )
        assert out.get("system_prompt") == "public prompt", f"Public loop should be readable; got {out!r}"

    def test_creator_can_read_own_private_loop(self, db_session):
        owner, creator = _make_creator_user(db_session, "loop-owner3-p0@example.com")
        loop = Loop(
            id=uuid4(),
            slug=f"mine-loop-{uuid4().hex[:8]}",
            title="Mine",
            is_public=False,
            creator_id=creator.id,
            success_condition="ok",
            verification_script="echo ok",
            max_turns=10,
            stopping_criteria={},
            tool_allowlist=[],
            system_prompt="mine",
        )
        db_session.add(loop)
        db_session.commit()

        caller = {
            "scope": "user",
            "user_id": owner.id,
            "api_key_id": None,
            "auth_ctx": AuthContext(scope="user", user_id=owner.id),
        }
        out = call_tool_sync("loopskill_get_loop", {"slug": loop.slug}, caller=caller, db=db_session)
        assert out.get("system_prompt") == "mine", f"Owner should read their own loop; got {out!r}"


class TestPrivatePersonalityLeak:
    def test_red_private_personality_secrets_not_leaked_to_stranger(self, db_session):
        _owner, creator = _make_creator_user(db_session, "pers-owner-p0@example.com")
        personality = Personality(
            id=uuid4(),
            slug=f"private-pers-{uuid4().hex[:8]}",
            title="Private Personality",
            is_public=False,
            creator_id=creator.id,
            system_prompt="SECRET SOUL PROMPT",
            config={"api_key": "sk-secret"},
        )
        db_session.add(personality)
        db_session.commit()

        out = call_tool_sync(
            "loopskill_get_personality", {"slug": personality.slug}, caller=_ATTACKER_CALLER, db=db_session
        )
        assert out.get("system_prompt") is None, (
            f"LEAK: private personality system_prompt exposed to a stranger; got {out!r}"
        )
        assert out.get("config") is None, (
            f"LEAK: private personality config exposed to a stranger; got {out!r}"
        )

    def test_public_personality_still_readable_by_anyone(self, db_session):
        _owner, creator = _make_creator_user(db_session, "pers-owner2-p0@example.com")
        personality = Personality(
            id=uuid4(),
            slug=f"public-pers-{uuid4().hex[:8]}",
            title="Public Personality",
            is_public=True,
            creator_id=creator.id,
            system_prompt="public soul",
        )
        db_session.add(personality)
        db_session.commit()

        out = call_tool_sync(
            "loopskill_get_personality", {"slug": personality.slug}, caller=_ATTACKER_CALLER, db=db_session
        )
        assert out.get("system_prompt") == "public soul", f"Public personality should be readable; got {out!r}"


class TestPrivateCompositeLoopLeak:
    def test_red_private_composite_loop_secrets_not_leaked_to_stranger(self, db_session):
        _owner, creator = _make_creator_user(db_session, "cl-owner-p0@example.com")
        cl = CompositeLoop(
            id=uuid4(),
            slug=f"private-cl-{uuid4().hex[:8]}",
            title="Private Composite Loop",
            is_public=False,
            creator_id=creator.id,
            schedule="1h",
            skills=[{"slug": "secret-internal-skill"}],
            connectors=[{"slug": "secret-connector", "pat": "gh_secret"}],
            subagents_config={"maker": {"system_prompt": "SECRET"}},
            verifier_slug="does-not-matter",
            state_seed={},
            prompt="SECRET DRIVING PROMPT",
        )
        db_session.add(cl)
        db_session.commit()

        out = call_tool_sync(
            "loopskill_get_composite_loop", {"slug": cl.slug}, caller=_ATTACKER_CALLER, db=db_session
        )
        assert out.get("prompt") is None, (
            f"LEAK: private composite loop prompt exposed to a stranger; got {out!r}"
        )
        assert out.get("connectors") is None, (
            f"LEAK: private composite loop connectors (with secrets) exposed to a stranger; got {out!r}"
        )

    def test_public_composite_loop_still_readable_by_anyone(self, db_session):
        _owner, creator = _make_creator_user(db_session, "cl-owner2-p0@example.com")
        cl = CompositeLoop(
            id=uuid4(),
            slug=f"public-cl-{uuid4().hex[:8]}",
            title="Public Composite Loop",
            is_public=True,
            creator_id=creator.id,
            schedule="1h",
            skills=[],
            connectors=[],
            subagents_config={},
            verifier_slug="does-not-matter",
            state_seed={},
            prompt="public prompt",
        )
        db_session.add(cl)
        db_session.commit()

        out = call_tool_sync(
            "loopskill_get_composite_loop", {"slug": cl.slug}, caller=_ATTACKER_CALLER, db=db_session
        )
        assert out.get("prompt") == "public prompt", f"Public composite loop should be readable; got {out!r}"
