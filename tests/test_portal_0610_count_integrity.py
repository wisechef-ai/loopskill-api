"""portal_0610 count-integrity hotfixes — B3, R5, R7, R2 (§6.6).

B3: /api/stats install counts (total, 7d, top_installed) excluded NO test/CI
    installs → polluted the public transparency numbers + the GTM kill-signal.
R5: /api/stats total_skills (+ by_tier/by_category) counted archived skills →
    disagreed with search/marketing (76 vs 72).
R7: public cookbook card summed each member skill's GLOBAL install count, so a
    skill shared across N cookbooks was counted N times. Now counts installs
    attributed to THIS cookbook via InstallEvent.cookbook_id.
R2: ?ref creator-handle attribution was silently dropped (allowlist only knew
    platform codes). Now creator handles validate + record.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_skill(db, slug, tier="free", is_public=True, is_archived=False):
    from app.models import Skill

    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        description="t",
        category="devops",
        tier=tier,
        is_public=is_public,
        is_archived=is_archived,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    return sk


def _mk_key(db, *, is_test):
    from app.models import APIKey, User

    u = User(id=uuid.uuid4(), display_name="u", email=f"{uuid.uuid4().hex[:8]}@e.com")
    db.add(u)
    db.flush()
    k = APIKey(
        id=uuid.uuid4(),
        user_id=u.id,
        key_prefix="k" * 8,
        key_hash=uuid.uuid4().hex,
        is_active=True,
        is_test=is_test,
    )
    db.add(k)
    db.flush()
    return k


def _mk_install(db, skill, *, key=None, cookbook_id=None):
    from app.models import InstallEvent

    db.add(
        InstallEvent(
            id=uuid.uuid4(),
            skill_id=skill.id,
            skill_slug=skill.slug,
            api_key_id=key.id if key else None,
            version_semver="1.0.0",
            bundle_id=cookbook_id,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.flush()


# ── B3: /api/stats excludes test installs ──────────────────────────────────


def test_b3_stats_excludes_test_installs(middleware_client, db_session):
    sk = _mk_skill(db_session, "b3-skill")
    organic = _mk_key(db_session, is_test=False)
    synthetic = _mk_key(db_session, is_test=True)
    _mk_install(db_session, sk, key=organic)
    _mk_install(db_session, sk, key=organic)
    _mk_install(db_session, sk, key=synthetic)  # must be excluded
    _mk_install(db_session, sk, key=None)  # anon = organic

    resp = middleware_client.get("/api/stats")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 3 organic (2 keyed + 1 anon), 1 test excluded
    assert body["total_installs_lifetime"] == 3
    assert body["installs_last_7d"] == 3
    top = {t["slug"]: t["installs"] for t in body["top_installed"]}
    assert top.get("b3-skill") == 3


# ── R5: /api/stats excludes archived skills ────────────────────────────────


def test_r5_stats_excludes_archived_skills(middleware_client, db_session):
    _mk_skill(db_session, "r5-live", tier="free")
    _mk_skill(db_session, "r5-archived", tier="free", is_archived=True)

    resp = middleware_client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()
    # only the live skill counts
    assert body["total_skills"] == 1
    assert body["by_tier"].get("free") == 1


# ── R7: cookbook card counts cookbook-attributed installs only ─────────────


def test_r7_cookbook_card_no_double_count(middleware_client, db_session):
    from app.models import Cookbook, CookbookSkill

    owner = uuid.uuid4()
    shared = _mk_skill(db_session, "r7-shared")
    # Two cookbooks both contain the shared skill.
    cb_a = Cookbook(id=uuid.uuid4(), name="A", slug="r7-a", visibility="public", bundle_owner=owner)
    cb_b = Cookbook(id=uuid.uuid4(), name="B", slug="r7-b", visibility="public", bundle_owner=owner)
    db_session.add_all([cb_a, cb_b])
    db_session.flush()
    db_session.add(CookbookSkill(bundle_id=cb_a.id, skill_id=shared.id, source="custom-added"))
    db_session.add(CookbookSkill(bundle_id=cb_b.id, skill_id=shared.id, source="custom-added"))
    db_session.flush()

    organic = _mk_key(db_session, is_test=False)
    # 5 installs attributed to A, 2 to B.
    for _ in range(5):
        _mk_install(db_session, shared, key=organic, cookbook_id=cb_a.id)
    for _ in range(2):
        _mk_install(db_session, shared, key=organic, cookbook_id=cb_b.id)

    from app.bundle_routes import _public_cb_card

    card_a = _public_cb_card(db_session, cb_a)
    card_b = _public_cb_card(db_session, cb_b)
    # Each cookbook shows ONLY its own attributed installs — not the shared global sum (7).
    assert card_a["installs_total"] == 5
    assert card_b["installs_total"] == 2


# ── R2: creator-handle ref validates + records ─────────────────────────────


def test_r2_creator_handle_ref_resolves(db_session):
    from app._skill_helpers import _resolve_ref_value
    from app.models import Creator, User

    u = User(id=uuid.uuid4(), display_name="creator", email=f"{uuid.uuid4().hex[:8]}@e.com")
    db_session.add(u)
    db_session.flush()
    db_session.add(Creator(id=uuid.uuid4(), user_id=u.id, name="C", slug="c-slug", handle="adamk"))
    db_session.flush()

    # platform code still works
    assert _resolve_ref_value("x", db=db_session) == "x"
    # a real creator handle now resolves (was dropped before)
    assert _resolve_ref_value("adamk", db=db_session) == "creator:adamk"
    # already-namespaced form is accepted
    assert _resolve_ref_value("creator:adamk", db=db_session) == "creator:adamk"
    # unknown handle is still dropped
    assert _resolve_ref_value("nobody-handle", db=db_session) is None
    # no db → only platform codes (backward-compatible)
    assert _resolve_ref_value("adamk", db=None) is None
    assert _resolve_ref_value("x", db=None) == "x"


def test_r2_cookbook_card_emits_handle(db_session):
    from app.bundle_routes import _public_cb_card
    from app.models import Cookbook, Creator, User

    u = User(id=uuid.uuid4(), display_name="creator", email=f"{uuid.uuid4().hex[:8]}@e.com")
    db_session.add(u)
    db_session.flush()
    db_session.add(Creator(id=uuid.uuid4(), user_id=u.id, name="C", slug="c2-slug", handle="adamk2"))
    cb = Cookbook(id=uuid.uuid4(), name="cb", slug="r2-cb", visibility="public", bundle_owner=u.id)
    db_session.add(cb)
    db_session.flush()

    card = _public_cb_card(db_session, cb)
    assert card["ref"] == "adamk2", "card must emit the creator handle, not the owner UUID"
