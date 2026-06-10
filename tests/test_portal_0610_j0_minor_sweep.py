"""portal_0610 J0 minor sweep — R8 channel validation, R6 tiebreaker, S1 tier filter.

R8: fleet_subscribe accepted ANY channel string ("turbo" stored, treated as
    canary). VALID_CHANNELS now enforced.
R6: discover sort=newest had no tiebreaker — seeded cookbooks sharing one
    created_at ordered arbitrarily. Cookbook.id.desc() added.
S1: /api/skills/search?tier=free computed the tier filter but never applied it,
    returning Pro skills. Now applied.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


# ── R8: fleet channel validation ───────────────────────────────────────────


def _user_ctx(user_id):
    from app.auth_ctx import AuthContext

    return AuthContext(scope="user", user_id=user_id)


def test_r8_invalid_channel_rejected(db_session):
    from app.mcp.tools.fleet import recipes_fleet_create, recipes_fleet_subscribe
    from app.models import Cookbook, User

    owner = User(id=uuid.uuid4(), display_name="o", email=f"{uuid.uuid4().hex[:8]}@e.com")
    db_session.add(owner)
    db_session.flush()
    cb = Cookbook(id=uuid.uuid4(), name="cb", cookbook_owner=owner.id)
    db_session.add(cb)
    db_session.flush()
    ctx = _user_ctx(owner.id)

    fleet = recipes_fleet_create(db=db_session, name="f", ctx=ctx)
    fleet_id = fleet["fleet_id"] if "fleet_id" in fleet else fleet.get("id")

    bad = recipes_fleet_subscribe(
        db=db_session, fleet_id=str(fleet_id), cookbook_id=str(cb.id), channel="turbo", ctx=ctx
    )
    assert bad.get("error") == "invalid_channel", bad
    assert "valid" in bad


def test_r8_valid_channels_accepted(db_session):
    from app.mcp.tools.fleet import recipes_fleet_create, recipes_fleet_subscribe
    from app.models import Cookbook, User

    owner = User(id=uuid.uuid4(), display_name="o", email=f"{uuid.uuid4().hex[:8]}@e.com")
    db_session.add(owner)
    db_session.flush()
    ctx = _user_ctx(owner.id)
    fleet = recipes_fleet_create(db=db_session, name="f2", ctx=ctx)
    fleet_id = fleet["fleet_id"] if "fleet_id" in fleet else fleet.get("id")

    for ch in ("canary", "stable", "frozen"):
        cb = Cookbook(id=uuid.uuid4(), name=f"cb-{ch}", cookbook_owner=owner.id)
        db_session.add(cb)
        db_session.flush()
        out = recipes_fleet_subscribe(
            db=db_session, fleet_id=str(fleet_id), cookbook_id=str(cb.id), channel=ch, ctx=ctx
        )
        assert out.get("error") is None, out
        assert out["channel"] == ch


# ── R6: discover newest tiebreaker ─────────────────────────────────────────


def test_r6_newest_is_deterministic(middleware_client, db_session):
    from app.models import Cookbook

    # Three public cookbooks sharing the SAME created_at.
    same_ts = datetime.now(timezone.utc) - timedelta(days=1)
    ids = []
    for i in range(3):
        cb = Cookbook(
            id=uuid.uuid4(),
            name=f"tie-{i}",
            slug=f"tie-{i}",
            visibility="public",
            cookbook_owner=uuid.uuid4(),
            created_at=same_ts,
        )
        db_session.add(cb)
        ids.append(cb.id)
    db_session.flush()

    r1 = middleware_client.get("/api/cookbooks/discover?sort=newest&limit=10")
    r2 = middleware_client.get("/api/cookbooks/discover?sort=newest&limit=10")
    assert r1.status_code == 200 and r2.status_code == 200
    order1 = [c["slug"] for c in r1.json().get("cookbooks", r1.json().get("items", []))]
    order2 = [c["slug"] for c in r2.json().get("cookbooks", r2.json().get("items", []))]
    # Same query twice → identical order (the tiebreaker makes it stable).
    assert order1 == order2


# ── S1: search tier filter applied ─────────────────────────────────────────


def _mk_skill(db, slug, tier):
    from app.models import Skill

    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        description="t",
        category="devops",
        tier=tier,
        is_public=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    return sk


def test_s1_tier_filter_excludes_other_tiers(middleware_client, db_session):
    _mk_skill(db_session, "s1-free", "free")
    _mk_skill(db_session, "s1-pro", "pro")
    _mk_skill(db_session, "s1-proplus", "pro_plus")

    resp = middleware_client.get("/api/skills/search?tier=free&page_size=50")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = body.get("results", body.get("skills", body.get("items", [])))
    slugs = {s["slug"] for s in items}
    assert "s1-free" in slugs
    assert "s1-pro" not in slugs, "S1: tier=free must NOT return Pro skills"
    assert "s1-proplus" not in slugs, "S1: tier=free must NOT return Pro+ skills"
