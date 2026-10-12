"""Issues #399 and #401 — two agent-facing papercuts found by perclient_1110.

#399  The private-bundle cap refusal said ``tier 'None'`` for agent principals
      (``users.subscription_tier IS NULL``). The gate itself already treats a
      NULL tier as ``free``; the message must say so too.
#401  ``loopskill_like`` took only a UUID, so ``id: "humanizer"`` (the thing an
      agent holds after ``loopskill_search``) answered ``invalid_id``. A skill
      slug now resolves; every other type still needs the UUID.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.auth_ctx import AuthContext
from app.mcp.registry import _tool_definitions
from app.mcp.server import _dispatch
from app.mcp.tools.bundle_install import CookbookInstallError
from app.mcp.tools.bundle_stream import loopskill_compose_bundle_from_links
from app.models import Bundle, BundleSkill, Skill, User
from app.tier_labels import effective_tier


def _user(db, tier):
    u = User(id=uuid4(), email=f"{uuid4()}@t.example", display_name="t", subscription_tier=tier)
    db.add(u)
    db.flush()
    return u


def _source_bundle(db, owner):
    skill = Skill(slug=f"s-{uuid4().hex[:8]}", title="s", is_public=True)
    src = Bundle(
        id=uuid4(), name="src", bundle_owner=owner.id, slug=f"src-{uuid4().hex[:8]}", visibility="public"
    )
    db.add_all([skill, src])
    db.flush()
    db.add(BundleSkill(bundle_id=src.id, skill_id=skill.id, source="custom-added"))
    db.flush()
    return src


# ───────────────────────── #399 ─────────────────────────


@pytest.mark.parametrize("raw,label", [(None, "free"), ("", "free"), ("free", "free"), ("pro", "pro")])
def test_effective_tier_resolves_null_to_free(raw, label):
    assert effective_tier(raw) == label


def test_effective_tier_maps_legacy_slugs():
    assert effective_tier("cook") == "pro"
    assert effective_tier("operator") == "pro_plus"


@pytest.mark.parametrize("db_tier", [None, "free"])
def test_cap_refusal_never_says_none(db_session, db_tier):
    user = _user(db_session, db_tier)
    for i in range(2):
        db_session.add(
            Bundle(
                id=uuid4(),
                name=f"p{i}",
                bundle_owner=user.id,
                slug=f"p{i}-{uuid4().hex[:6]}",
                visibility="private",
            )
        )
    src = _source_bundle(db_session, user)
    db_session.commit()

    # ctx.tier is exactly what the agent-principal auth path produces: None.
    ctx = AuthContext(scope="user", user_id=user.id, tier=db_tier)
    with pytest.raises(CookbookInstallError) as ei:
        loopskill_compose_bundle_from_links(db_session, links=[f"bundle://{src.slug}"], ctx=ctx)
    assert ei.value.code == "cookbook_limit"
    assert "'None'" not in ei.value.message
    assert "tier 'free'" in ei.value.message
    assert "(2/2)" in ei.value.message


# ───────────────────────── #401 ─────────────────────────


def _like(db, ctx, **args):
    return _dispatch("loopskill_like", db, {"action": "like", "type": "skill", **args}, {"auth_ctx": ctx})


def test_like_accepts_a_skill_slug(db_session):
    user = _user(db_session, "free")
    skill = Skill(slug=f"humanizer-{uuid4().hex[:6]}", title="h", is_public=True)
    db_session.add(skill)
    db_session.commit()
    ctx = AuthContext(scope="user", user_id=user.id, tier="free")

    out = _like(db_session, ctx, id=skill.slug)
    assert out == {"liked": True, "type": "skill", "id": str(skill.id)}
    # Same answer by UUID: idempotent, one row.
    assert _like(db_session, ctx, id=str(skill.id)) == out


def test_like_unknown_slug_is_not_found_not_invalid_id(db_session):
    user = _user(db_session, "free")
    ctx = AuthContext(scope="user", user_id=user.id, tier="free")
    out = _like(db_session, ctx, id="no-such-skill-" + uuid4().hex)
    assert out["error"] == "not_found"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 300, "a b", "../etc/passwd", "'; drop table skills;--"])
def test_like_garbage_ids_are_rejected_without_a_lookup_blowup(db_session, bad):
    user = _user(db_session, "free")
    ctx = AuthContext(scope="user", user_id=user.id, tier="free")
    out = _like(db_session, ctx, id=bad)
    assert out["error"] in {"invalid_id", "not_found"}


def test_like_slug_is_skill_only(db_session):
    """personality / loop keep requiring the UUID (their slugs are not the contract)."""
    user = _user(db_session, "free")
    ctx = AuthContext(scope="user", user_id=user.id, tier="free")
    out = _dispatch(
        "loopskill_like", db_session, {"action": "like", "type": "loop", "id": "some-loop"}, {"auth_ctx": ctx}
    )
    assert out["error"] == "invalid_id"


def test_like_schema_says_uuid_or_slug():
    tools = {t.name: t for t in _tool_definitions()}
    desc = tools["loopskill_like"].inputSchema["properties"]["id"]["description"]
    assert "slug" in desc.lower() and "uuid" in desc.lower()


def test_like_private_skill_of_someone_else_is_indistinguishable_from_absent(db_session):
    """A slug must not be an existence oracle for another user's private skill."""
    owner = _user(db_session, "free")
    stranger = _user(db_session, "free")
    private = Skill(slug=f"priv-{uuid4().hex[:6]}", title="p", is_public=False)
    own_bundle = Bundle(
        id=uuid4(), name="mine", bundle_owner=owner.id, slug=f"mine-{uuid4().hex[:6]}", visibility="private"
    )
    db_session.add_all([private, own_bundle])
    db_session.flush()
    db_session.add(BundleSkill(bundle_id=own_bundle.id, skill_id=private.id, source="custom-added"))
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=stranger.id, tier="free")
    leaked = _like(db_session, ctx, id=private.slug)
    absent = _like(db_session, ctx, id="priv-" + uuid4().hex[:6])
    assert leaked["error"] == absent["error"] == "not_found"

    # The owner can still like their own private skill by slug.
    ctx_owner = AuthContext(scope="user", user_id=owner.id, tier="free")
    assert _like(db_session, ctx_owner, id=private.slug)["liked"] is True


def test_like_slug_is_whitespace_trimmed_and_unlike_by_slug_works(db_session):
    user = _user(db_session, "free")
    skill = Skill(slug=f"trim-{uuid4().hex[:6]}", title="t", is_public=True)
    db_session.add(skill)
    db_session.commit()
    ctx = AuthContext(scope="user", user_id=user.id, tier="free")
    assert _like(db_session, ctx, id=f"  {skill.slug}  ")["liked"] is True
    out = _dispatch(
        "loopskill_like",
        db_session,
        {"action": "unlike", "type": "skill", "id": skill.slug},
        {"auth_ctx": ctx},
    )
    assert out == {"liked": False, "type": "skill", "id": str(skill.id)}


def test_like_non_string_id_does_not_crash(db_session):
    user = _user(db_session, "free")
    ctx = AuthContext(scope="user", user_id=user.id, tier="free")
    for bad in (None, 123, ["a"], {"x": 1}):
        out = _like(db_session, ctx, id=bad)
        assert out["error"] == "invalid_id"
