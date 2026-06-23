"""Tests for spotify_0608 Ph D — streaming MCP cookbook-composition verbs.

Covers the three verbs in app/mcp/tools/cookbook_stream.py:
  - recipes_install_from_cookbook  (install from a public cookbook link)
  - recipes_pick_best_from_cookbook (best skill for a need from a link)
  - recipes_compose_cookbook_from_links (compose a new owned cookbook from N links)

Plus the link grammar (_parse_link / _strip_ref) and the dispatch wiring
(registry tool defs + server _dispatch branches).
"""

from __future__ import annotations

import uuid
from typing import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.mcp.tools.bundle_install import CookbookInstallError
from app.mcp.tools.bundle_stream import (
    LINK_BARE,
    LINK_COOKBOOK,
    LINK_EXTERNAL,
    LINK_SKILL,
    _parse_link,
    _relevance,
    _strip_ref,
    recipes_compose_cookbook_from_links,
    recipes_install_from_cookbook,
    recipes_pick_best_from_cookbook,
)
from app.models import (
    Base,
    Cookbook,
    CookbookSkill,
    InstallEvent,
    Skill,
    SkillVersion,
    User,
)


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _mk_user(db, tier="pro"):
    u = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int) % 1_000_000_000,
        email=f"u-{uuid.uuid4().hex[:6]}@t.io",
        display_name="u",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.commit()
    return u


def _mk_cookbook(db, owner, slug, visibility="public", name="CB"):
    cb = Cookbook(
        id=uuid.uuid4(),
        name=name,
        bundle_owner=owner.id if owner else None,
        slug=slug,
        visibility=visibility,
    )
    db.add(cb)
    db.commit()
    return cb


def _mk_skill(db, slug, title=None, description=None, public=True):
    s = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=title or slug,
        description=description,
        is_public=public,
    )
    db.add(s)
    db.commit()
    return s


def _mk_version(db, skill, semver="1.0.0"):
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=skill.id,
        semver=semver,
        checksum_sha256="a" * 64,
    )
    db.add(v)
    db.commit()
    return v


def _attach(db, cb, skill, source="custom-added"):
    db.add(CookbookSkill(bundle_id=cb.id, skill_id=skill.id, source=source))
    db.commit()


def _install(db, skill, *, is_test=False, n=1):
    for _ in range(n):
        db.add(
            InstallEvent(
                id=uuid.uuid4(),
                skill_id=skill.id,
                skill_slug=skill.slug,
                version_semver="1.0.0",
            )
        )
    db.commit()


def _user_ctx(user, tier="pro"):
    return AuthContext(scope="user", user_id=user.id, tier=tier)


# ── Link grammar ─────────────────────────────────────────────────────────────


def test_strip_ref_drops_query_and_fragment():
    assert _strip_ref("cookbook://foo?ref=alice") == "cookbook://foo"
    assert _strip_ref("foo#frag") == "foo"
    assert _strip_ref("  bar  ") == "bar"


def test_parse_link_cookbook_scheme():
    assert _parse_link("cookbook://my-stack") == (LINK_COOKBOOK, "my-stack")
    assert _parse_link("cookbook:my-stack") == (LINK_COOKBOOK, "my-stack")
    assert _parse_link("cookbook://my-stack?ref=alice") == (LINK_COOKBOOK, "my-stack")


def test_parse_link_skill_scheme():
    assert _parse_link("skill://summarize-cli") == (LINK_SKILL, "summarize-cli")
    assert _parse_link("recipes:summarize-cli") == (LINK_SKILL, "summarize-cli")


def test_parse_link_external_known_source(monkeypatch):
    monkeypatch.setattr(
        "app.mcp.tools.bundle_stream.known_external_source",
        lambda s: s == "clawhub",
    )
    assert _parse_link("clawhub:web-scraper") == (LINK_EXTERNAL, "clawhub", "web-scraper")


def test_parse_link_unknown_scheme_falls_back_to_bare(monkeypatch):
    monkeypatch.setattr("app.mcp.tools.bundle_stream.known_external_source", lambda s: False)
    # A slug that happens to contain a colon → whole thing is a bare token.
    assert _parse_link("weird:thing") == (LINK_BARE, "weird:thing")


def test_parse_link_bare():
    assert _parse_link("just-a-slug") == (LINK_BARE, "just-a-slug")


def test_parse_link_empty_raises():
    with pytest.raises(CookbookInstallError):
        _parse_link("")
    with pytest.raises(CookbookInstallError):
        _parse_link("cookbook://")
    with pytest.raises(CookbookInstallError):
        _parse_link("   ")


def test_relevance_scoring():
    s = _mk_skill_obj("pr-draft", "PR Draft Writer", "generates pull request descriptions")
    assert _relevance(s, "pr-draft") == 2  # slug hit
    assert _relevance(s, "PR Draft") == 2  # title hit
    assert _relevance(s, "pull request") == 1  # desc token hit
    assert _relevance(s, "kubernetes") == 0  # no hit
    assert _relevance(s, "") == 0


def _mk_skill_obj(slug, title, desc):
    s = Skill(id=uuid.uuid4(), slug=slug, title=title, description=desc, is_public=True)
    return s


# ── Verb 1: install_from_cookbook ────────────────────────────────────────────


def test_install_from_cookbook_returns_all_skills_and_clone_line(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner, "awakened-agent")
    s1 = _mk_skill(db, "summarize-cli")
    s2 = _mk_skill(db, "super-memory")
    _mk_version(db, s1)
    _mk_version(db, s2)
    _attach(db, cb, s1)
    _attach(db, cb, s2)

    out = recipes_install_from_cookbook(db, link="cookbook://awakened-agent")
    assert out["slug"] == "awakened-agent"
    assert {s["slug"] for s in out["skills"]} == {"summarize-cli", "super-memory"}
    assert all(s["tarball_url"] for s in out["skills"])
    assert "cookbook://awakened-agent" in out["clone_line"]
    assert f"?ref={owner.id}" in out["clone_line"]
    # install events recorded for both
    assert db.query(InstallEvent).count() == 2


def test_install_from_cookbook_bare_slug(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner, "bare-stack")
    s1 = _mk_skill(db, "tool-a")
    _mk_version(db, s1)
    _attach(db, cb, s1)
    out = recipes_install_from_cookbook(db, link="bare-stack")
    assert out["slug"] == "bare-stack"
    assert len(out["skills"]) == 1


def test_install_from_cookbook_private_is_404(db):
    owner = _mk_user(db)
    _mk_cookbook(db, owner, "secret", visibility="private")
    with pytest.raises(CookbookInstallError) as ei:
        recipes_install_from_cookbook(db, link="cookbook://secret")
    assert ei.value.status == 404


def test_install_from_cookbook_rejects_skill_link(db):
    with pytest.raises(CookbookInstallError) as ei:
        recipes_install_from_cookbook(db, link="skill://summarize-cli")
    assert ei.value.code == "not_a_cookbook_link"


def test_install_from_cookbook_skips_disabled(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner, "with-disabled")
    s1 = _mk_skill(db, "live")
    s2 = _mk_skill(db, "dead")
    _mk_version(db, s1)
    _mk_version(db, s2)
    _attach(db, cb, s1)
    _attach(db, cb, s2, source="disabled")
    out = recipes_install_from_cookbook(db, link="cookbook://with-disabled")
    assert {s["slug"] for s in out["skills"]} == {"live"}


# ── Verb 2: pick_best_from_cookbook ──────────────────────────────────────────


def test_pick_best_by_need_relevance(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner, "tools")
    s1 = _mk_skill(db, "pr-draft", "PR Draft Writer", "writes pull request bodies")
    s2 = _mk_skill(db, "summarize-cli", "Summarize CLI", "summarize any url")
    _mk_version(db, s1)
    _mk_version(db, s2)
    _attach(db, cb, s1)
    _attach(db, cb, s2)
    out = recipes_pick_best_from_cookbook(db, link="cookbook://tools", need="pull request")
    assert out["picked"]["slug"] == "pr-draft"
    assert out["picked"]["relevance"] >= 1
    assert "install_line" in out["picked"]


def test_pick_best_ties_broken_by_installs(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner, "popular")
    s1 = _mk_skill(db, "low", "Low", "memory tool")
    s2 = _mk_skill(db, "high", "High", "memory tool")
    _mk_version(db, s1)
    _mk_version(db, s2)
    _attach(db, cb, s1)
    _attach(db, cb, s2)
    _install(db, s2, n=5)  # s2 more installed; both equally relevant
    out = recipes_pick_best_from_cookbook(db, link="cookbook://popular", need="memory")
    assert out["picked"]["slug"] == "high"


def test_pick_best_no_need_ranks_by_installs(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner, "noneed")
    s1 = _mk_skill(db, "a")
    s2 = _mk_skill(db, "b")
    _mk_version(db, s1)
    _mk_version(db, s2)
    _attach(db, cb, s1)
    _attach(db, cb, s2)
    _install(db, s1, n=3)
    out = recipes_pick_best_from_cookbook(db, link="cookbook://noneed")
    assert out["picked"]["slug"] == "a"


def test_pick_best_need_with_no_match_falls_back_to_popularity(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner, "fallback")
    s1 = _mk_skill(db, "alpha", "Alpha", "nothing relevant")
    _mk_version(db, s1)
    _attach(db, cb, s1)
    out = recipes_pick_best_from_cookbook(db, link="cookbook://fallback", need="zzz-unmatchable")
    assert out["picked"]["slug"] == "alpha"  # fell back, didn't return None


def test_pick_best_empty_cookbook_returns_none(db):
    owner = _mk_user(db)
    _mk_cookbook(db, owner, "empty")
    out = recipes_pick_best_from_cookbook(db, link="cookbook://empty")
    assert out["picked"] is None
    assert out["ranked"] == []


def test_pick_best_does_not_record_installs(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner, "noinstall")
    s1 = _mk_skill(db, "x")
    _mk_version(db, s1)
    _attach(db, cb, s1)
    recipes_pick_best_from_cookbook(db, link="cookbook://noinstall", need="x")
    assert db.query(InstallEvent).count() == 0  # picking != installing


# ── Verb 3: compose_cookbook_from_links ──────────────────────────────────────


def test_compose_unions_skills_from_multiple_links(db):
    owner = _mk_user(db)
    # source cookbook with 2 skills
    src = _mk_cookbook(db, owner, "src-stack")
    s1 = _mk_skill(db, "summarize-cli")
    s2 = _mk_skill(db, "super-memory")
    _attach(db, src, s1)
    _attach(db, src, s2)
    # standalone internal skill
    _mk_skill(db, "chef")

    ctx = _user_ctx(owner)
    out = recipes_compose_cookbook_from_links(
        db,
        links=["cookbook://src-stack", "skill://chef"],
        name="My Awakened Agent",
        ctx=ctx,
    )
    assert out["skill_count"] == 3
    assert {s["slug"] for s in out["skills"]} == {"summarize-cli", "super-memory", "chef"}
    assert out["name"] == "My Awakened Agent"
    # new cookbook persisted + owned by caller
    cb = db.query(Cookbook).filter(Cookbook.id == uuid.UUID(out["cookbook"])).first()
    assert cb is not None and cb.bundle_owner == owner.id
    assert cb.visibility == "private"


def test_compose_dedupes_overlapping_skills(db):
    owner = _mk_user(db)
    s1 = _mk_skill(db, "shared")
    cb_a = _mk_cookbook(db, owner, "a-stack")
    cb_b = _mk_cookbook(db, owner, "b-stack")
    _attach(db, cb_a, s1)
    _attach(db, cb_b, s1)
    ctx = _user_ctx(owner)
    out = recipes_compose_cookbook_from_links(db, links=["cookbook://a-stack", "cookbook://b-stack"], ctx=ctx)
    assert out["skill_count"] == 1  # deduped


def test_compose_partial_success_reports_bad_link(db):
    owner = _mk_user(db)
    s1 = _mk_skill(db, "good")
    cb = _mk_cookbook(db, owner, "good-stack")
    _attach(db, cb, s1)
    ctx = _user_ctx(owner)
    out = recipes_compose_cookbook_from_links(
        db, links=["cookbook://good-stack", "skill://does-not-exist"], ctx=ctx
    )
    assert out["skill_count"] == 1
    per = {entry["link"]: entry for entry in out["links"]}
    assert per["cookbook://good-stack"]["ok"] is True
    assert per["skill://does-not-exist"]["ok"] is False


def test_compose_all_links_dead_raises(db):
    owner = _mk_user(db)
    ctx = _user_ctx(owner)
    with pytest.raises(CookbookInstallError) as ei:
        recipes_compose_cookbook_from_links(db, links=["skill://nope"], ctx=ctx)
    assert ei.value.code == "no_skills_resolved"


def test_compose_requires_user_scope(db):
    with pytest.raises(CookbookInstallError) as ei:
        recipes_compose_cookbook_from_links(db, links=["skill://x"], ctx=AuthContext(scope="anonymous"))
    assert ei.value.status == 401


def test_compose_rejects_empty_links(db):
    owner = _mk_user(db)
    with pytest.raises(CookbookInstallError) as ei:
        recipes_compose_cookbook_from_links(db, links=[], ctx=_user_ctx(owner))
    assert ei.value.code == "no_links"


def test_compose_rejects_too_many_links(db):
    owner = _mk_user(db)
    with pytest.raises(CookbookInstallError) as ei:
        recipes_compose_cookbook_from_links(
            db, links=[f"skill://s{i}" for i in range(26)], ctx=_user_ctx(owner)
        )
    assert ei.value.code == "too_many_links"


def test_compose_honors_tier_cookbook_cap(db):
    # free tier = 1 cookbook; create one, then composing a 2nd must 403.
    owner = _mk_user(db, tier="free")
    _mk_cookbook(db, owner, "existing", visibility="private")
    s1 = _mk_skill(db, "z")
    src = _mk_cookbook(db, owner, "z-stack")
    _attach(db, src, s1)
    ctx = _user_ctx(owner, tier="free")
    with pytest.raises(CookbookInstallError) as ei:
        recipes_compose_cookbook_from_links(db, links=["cookbook://z-stack"], ctx=ctx)
    assert ei.value.code == "cookbook_limit"


def test_compose_bare_token_resolves_cookbook_then_skill(db):
    owner = _mk_user(db)
    _mk_skill(db, "barewin")
    ctx = _user_ctx(owner)
    out = recipes_compose_cookbook_from_links(db, links=["barewin"], ctx=ctx)
    assert out["skill_count"] == 1
    assert out["skills"][0]["slug"] == "barewin"


# ── Dispatch wiring ──────────────────────────────────────────────────────────


def test_verbs_registered_in_registry():
    from app.mcp.registry import _tool_definitions

    names = {t.name for t in _tool_definitions()}
    assert "recipes_install_from_cookbook" in names
    assert "recipes_pick_best_from_cookbook" in names
    assert "recipes_compose_cookbook_from_links" in names


def test_dispatch_routes_install_from_cookbook(db):
    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner, "disp-stack")
    s1 = _mk_skill(db, "d1")
    _mk_version(db, s1)
    _attach(db, cb, s1)
    from app.mcp.server import _dispatch

    out = _dispatch(
        "recipes_install_from_cookbook",
        db,
        {"link": "cookbook://disp-stack"},
        {"scope": "anonymous"},
    )
    assert out["slug"] == "disp-stack"


def test_dispatch_maps_error_envelope(db):
    from app.mcp.server import _dispatch

    out = _dispatch(
        "recipes_install_from_cookbook",
        db,
        {"link": "cookbook://missing"},
        {"scope": "anonymous"},
    )
    assert out["code"] == "cookbook_not_found"
    assert out["status"] == 404
