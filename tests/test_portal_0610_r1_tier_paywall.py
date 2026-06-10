"""portal_0610 R1 — P0 paywall-bypass closure (§6.6).

Live-reproduced on prod 2026-06-10: a FREE-tier authenticated API key
downloaded the COMPLETE `chef` (tier=pro) tarball (HTTP 200, real files). The
direct install route enforced visibility (public/private) but NOT tier-access,
and `TIER_RANK` was not even imported. Same gap on both cookbook install routes
and the MCP recipes_cookbook_install tool.

This suite pins the fix across ALL FOUR install surfaces:

  1. GET  /api/skills/install                         (direct)
  2. POST /api/cookbooks/{id}/install                 (cookbook bulk, HTTP)
  3. GET  /api/cookbooks/{id}/skills/{slug}/install   (cookbook single, HTTP)
  4. recipes_cookbook_install(...)                    (MCP tool)

Invariants:
  - free authenticated key → PRO skill (direct)            → 403 (was 200)
  - free authenticated key → FREE skill (direct)           → 200 (no regression)
  - pro  authenticated key → PRO skill (direct)            → 200 (no regression)
  - free-OWNER cookbook bulk install                       → PRO skills skipped,
                                                              FREE skills present
  - free-OWNER cookbook single PRO skill install (HTTP)    → 403
  - pro-OWNER  cookbook single PRO skill install (HTTP)    → 200
  - MCP bulk on free-owner cookbook                        → PRO skipped
  - MCP single PRO skill on free-owner cookbook            → 403 (tier_insufficient)

The pure rank predicate is also unit-tested in isolation.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


# ── pure-predicate unit tests (no DB) ──────────────────────────────────────


def test_tier_rank_predicate_free_cannot_install_pro():
    from app.authz import tier_rank_allows_install

    assert tier_rank_allows_install("free", "pro") is False
    assert tier_rank_allows_install("free", "pro_plus") is False
    assert tier_rank_allows_install("free", "free") is True
    assert tier_rank_allows_install("pro", "pro") is True
    assert tier_rank_allows_install("pro", "pro_plus") is False
    assert tier_rank_allows_install("pro_plus", "pro") is True


def test_tier_rank_predicate_none_floors_to_free():
    from app.authz import tier_rank_allows_install

    # None / unknown caller → may only reach free skills
    assert tier_rank_allows_install(None, "pro") is False
    assert tier_rank_allows_install(None, None) is True
    assert tier_rank_allows_install(None, "free") is True
    # None / unknown skill tier → treated as free (installable by anyone)
    assert tier_rank_allows_install("free", None) is True
    # legacy aliases resolve through TIER_RANK
    assert tier_rank_allows_install("free", "cook") is False  # cook == pro
    assert tier_rank_allows_install("pro", "cook") is True


# ── fixtures / seed helpers ────────────────────────────────────────────────


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_user(db, *, tier: str | None, status: str = "active"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name=f"user-{tier or 'none'}",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status=status if tier else None,
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user, *, raw: str | None = None):
    """Mint a real APIKey row whose sha256 matches the returned raw header."""
    from app.models import APIKey

    raw = raw or f"rec_{uuid.uuid4().hex}"
    k = APIKey(
        id=uuid.uuid4(),
        user_id=user.id,
        key_prefix=raw[:8],
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        name="r1-test",
        is_active=True,
        is_test=True,
    )
    db.add(k)
    db.flush()
    return raw


def _mk_skill(db, *, slug: str, tier: str, is_public: bool = True):
    from app.models import Skill, SkillVersion

    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug.replace("-", " ").title(),
        description=f"Test skill {slug}",
        tier=tier,
        is_public=is_public,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    v = SkillVersion(
        id=uuid.uuid4(),
        skill_id=sk.id,
        semver="1.0.0",
        tarball_size_bytes=1024,
        checksum_sha256="deadbeef" * 8,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.flush()
    return sk


def _mk_cookbook(db, *, owner, name: str = "deliverable"):
    from app.models import Cookbook

    cb = Cookbook(id=uuid.uuid4(), name=name, cookbook_owner=owner.id if owner else None)
    db.add(cb)
    db.flush()
    return cb


def _add_to_cookbook(db, cb, skill, source: str = "custom-added"):
    from app.models import CookbookSkill

    cs = CookbookSkill(
        cookbook_id=cb.id,
        skill_id=skill.id,
        source=source,
    )
    db.add(cs)
    db.flush()
    return cs


# ── Surface 1: direct /api/skills/install ──────────────────────────────────


def test_direct_free_key_pro_skill_403(middleware_client, db_session):
    """THE breach: free authenticated key must NOT pull a Pro tarball."""
    user = _mk_user(db_session, tier="free")
    key = _mk_key(db_session, user)
    _mk_skill(db_session, slug="chef-probe", tier="pro", is_public=True)

    resp = middleware_client.get(
        "/api/skills/install?slug=chef-probe", headers={"x-api-key": key}
    )
    assert resp.status_code == 403, (
        f"PAYWALL BYPASS: free key got {resp.status_code} on a Pro skill: {resp.text[:200]}"
    )


def test_direct_free_key_free_skill_200(middleware_client, db_session):
    """No regression: free key still installs free skills."""
    user = _mk_user(db_session, tier="free")
    key = _mk_key(db_session, user)
    _mk_skill(db_session, slug="free-probe", tier="free", is_public=True)

    resp = middleware_client.get(
        "/api/skills/install?slug=free-probe", headers={"x-api-key": key}
    )
    assert resp.status_code == 200, resp.text[:200]


def test_direct_pro_key_pro_skill_200(middleware_client, db_session):
    """No regression: a paying Pro key installs Pro skills."""
    user = _mk_user(db_session, tier="pro")
    key = _mk_key(db_session, user)
    _mk_skill(db_session, slug="pro-probe", tier="pro", is_public=True)

    resp = middleware_client.get(
        "/api/skills/install?slug=pro-probe", headers={"x-api-key": key}
    )
    assert resp.status_code == 200, resp.text[:200]


def test_direct_lapsed_pro_key_pro_skill_403(middleware_client, db_session):
    """A lapsed (canceled) Pro subscriber resolves to no-tier → blocked from Pro."""
    user = _mk_user(db_session, tier="pro", status="canceled")
    key = _mk_key(db_session, user)
    _mk_skill(db_session, slug="pro-lapsed-probe", tier="pro", is_public=True)

    resp = middleware_client.get(
        "/api/skills/install?slug=pro-lapsed-probe", headers={"x-api-key": key}
    )
    assert resp.status_code == 403, resp.text[:200]


# ── Surface 2 & 3: cookbook install routes (owner-tier scoped) ─────────────


def test_cookbook_bulk_free_owner_skips_pro(middleware_client, db_session):
    """A FREE-owner cookbook bulk install emits free skills, skips Pro skills."""
    owner = _mk_user(db_session, tier="free")
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner=owner)
    free_sk = _mk_skill(db_session, slug="cb-free", tier="free")
    pro_sk = _mk_skill(db_session, slug="cb-pro", tier="pro")
    _add_to_cookbook(db_session, cb, free_sk)
    _add_to_cookbook(db_session, cb, pro_sk)

    resp = middleware_client.post(
        f"/api/cookbooks/{cb.id}/install", headers={"x-api-key": key}
    )
    assert resp.status_code == 200, resp.text[:200]
    slugs = {s["slug"] for s in resp.json()["skills"]}
    assert "cb-free" in slugs
    assert "cb-pro" not in slugs, "Pro skill leaked from a free-owner cookbook bulk install"


def test_cookbook_single_free_owner_pro_skill_403(middleware_client, db_session):
    """Explicit single-skill install of a Pro skill from a free-owner cookbook → 403."""
    owner = _mk_user(db_session, tier="free")
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner=owner)
    pro_sk = _mk_skill(db_session, slug="cb-pro-single", tier="pro")
    _add_to_cookbook(db_session, cb, pro_sk)

    resp = middleware_client.get(
        f"/api/cookbooks/{cb.id}/skills/cb-pro-single/install",
        headers={"x-api-key": key},
    )
    assert resp.status_code == 403, resp.text[:200]


def test_cookbook_single_pro_owner_pro_skill_200(middleware_client, db_session):
    """A PRO-owner cookbook CAN hand out a Pro skill (the L10 tier-wide case)."""
    owner = _mk_user(db_session, tier="pro")
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner=owner)
    pro_sk = _mk_skill(db_session, slug="cb-pro-ok", tier="pro")
    _add_to_cookbook(db_session, cb, pro_sk)

    resp = middleware_client.get(
        f"/api/cookbooks/{cb.id}/skills/cb-pro-ok/install",
        headers={"x-api-key": key},
    )
    assert resp.status_code == 200, resp.text[:200]


# ── Surface 4: MCP recipes_cookbook_install ────────────────────────────────


def _user_ctx(user):
    from app.auth_ctx import AuthContext

    return AuthContext(scope="user", user_id=user.id)


def test_mcp_bulk_free_owner_skips_pro(db_session):
    from app.mcp.tools.cookbook_install import recipes_cookbook_install

    owner = _mk_user(db_session, tier="free")
    cb = _mk_cookbook(db_session, owner=owner)
    free_sk = _mk_skill(db_session, slug="mcp-free", tier="free")
    pro_sk = _mk_skill(db_session, slug="mcp-pro", tier="pro")
    _add_to_cookbook(db_session, cb, free_sk)
    _add_to_cookbook(db_session, cb, pro_sk)

    out = recipes_cookbook_install(db=db_session, ctx=_user_ctx(owner), cookbook_id=str(cb.id))
    slugs = {s["slug"] for s in out["skills"]}
    assert "mcp-free" in slugs
    assert "mcp-pro" not in slugs, "MCP bulk leaked a Pro skill from a free-owner cookbook"


def test_mcp_single_free_owner_pro_skill_raises(db_session):
    from app.mcp.tools.cookbook_install import CookbookInstallError, recipes_cookbook_install

    owner = _mk_user(db_session, tier="free")
    cb = _mk_cookbook(db_session, owner=owner)
    pro_sk = _mk_skill(db_session, slug="mcp-pro-single", tier="pro")
    _add_to_cookbook(db_session, cb, pro_sk)

    with pytest.raises(CookbookInstallError) as exc:
        recipes_cookbook_install(
            db=db_session, ctx=_user_ctx(owner), cookbook_id=str(cb.id), slug="mcp-pro-single"
        )
    assert exc.value.status == 403
    assert exc.value.code == "tier_insufficient"


def test_mcp_single_pro_owner_pro_skill_ok(db_session):
    from app.mcp.tools.cookbook_install import recipes_cookbook_install

    owner = _mk_user(db_session, tier="pro")
    cb = _mk_cookbook(db_session, owner=owner)
    pro_sk = _mk_skill(db_session, slug="mcp-pro-ok", tier="pro")
    _add_to_cookbook(db_session, cb, pro_sk)

    out = recipes_cookbook_install(
        db=db_session, ctx=_user_ctx(owner), cookbook_id=str(cb.id), slug="mcp-pro-ok"
    )
    assert out["slug"] == "mcp-pro-ok"
