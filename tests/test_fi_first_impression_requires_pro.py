"""fi_first_impression_api — `requires_pro` on the bundle discover feed and
public bundle page.

THE GAP
-------
Live audit (2026-08-19): a visitor browsing GET /api/bundles/discover or
GET /api/bundles/public/{slug} had no field telling them a bundle is
entirely Pro-locked — they'd only discover it after clicking through, or
(worse) after piping install.sh and getting a mystery "installed 0
skill(s)" (see tests/test_fi_first_impression_install_sh_pro_warning.py for
that sibling fix). `requires_pro` closes the gap on the READ side: computed
from member skill tiers, reusing the SAME free/lock predicates the
well-known index and install.sh already use
(`app.bundle_wellknown_routes._is_free` / `_is_redistributable_external`),
so the three surfaces (discover card, public page, install.sh) can never
disagree about which bundle is "free" vs "Pro-locked".

Semantics pinned here:
  * all members Pro-locked            -> requires_pro=True
  * at least one free/redistributable -> requires_pro=False
  * zero active members (empty bundle) -> requires_pro=False (not the same
    condition as "everything is paywalled" — nothing to install yet)
  * disabled members are excluded (mirrors _skills_for(include_disabled=False),
    the same set install.sh's well-known index walks)
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests._app_factory import build_test_app


def _mk_bundle(db, *, name="Pro Gate Bundle", slug=None, visibility="public", **kw):
    from app.models import Bundle

    b = Bundle(id=uuid.uuid4(), name=name, slug=slug, visibility=visibility, is_base=False, **kw)
    db.add(b)
    db.flush()
    return b


def _mk_skill(db, *, slug, tier="free"):
    from datetime import datetime, timezone

    from app.models import Skill

    s = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        tier=tier,
        is_public=True,
        created_at=datetime.now(timezone.utc),
        readme=f"# {slug}\n\nBody for {slug}.",
    )
    db.add(s)
    db.flush()
    return s


def _link(db, bundle, skill, *, source="custom-added"):
    from app.models import BundleSkill

    db.add(BundleSkill(bundle_id=bundle.id, skill_id=skill.id, source=source))


@pytest.fixture
def app_client(db_session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, raise_server_exceptions=True)


# ═════════════════════════════════════════════════════════════════════════
# GET /api/bundles/discover
# ═════════════════════════════════════════════════════════════════════════


def test_discover_flags_all_pro_bundle_as_requires_pro(app_client, db_session):
    b = _mk_bundle(db_session, name="All Pro", slug="all-pro-discover")
    _link(db_session, b, _mk_skill(db_session, slug="pro-a-discover", tier="pro"))
    _link(db_session, b, _mk_skill(db_session, slug="pro-b-discover", tier="pro"))
    db_session.commit()

    resp = app_client.get("/api/bundles/discover")
    assert resp.status_code == 200, resp.text
    cards = {c["slug"]: c for c in resp.json()["bundles"]}
    assert cards["all-pro-discover"]["requires_pro"] is True


def test_discover_does_not_flag_mixed_bundle(app_client, db_session):
    b = _mk_bundle(db_session, name="Mixed", slug="mixed-discover")
    _link(db_session, b, _mk_skill(db_session, slug="free-a-discover", tier="free"))
    _link(db_session, b, _mk_skill(db_session, slug="pro-c-discover", tier="pro"))
    db_session.commit()

    resp = app_client.get("/api/bundles/discover")
    cards = {c["slug"]: c for c in resp.json()["bundles"]}
    assert cards["mixed-discover"]["requires_pro"] is False


def test_discover_does_not_flag_all_free_bundle(app_client, db_session):
    b = _mk_bundle(db_session, name="All Free", slug="all-free-discover")
    _link(db_session, b, _mk_skill(db_session, slug="free-b-discover", tier="free"))
    db_session.commit()

    resp = app_client.get("/api/bundles/discover")
    cards = {c["slug"]: c for c in resp.json()["bundles"]}
    assert cards["all-free-discover"]["requires_pro"] is False


def test_discover_empty_bundle_is_not_requires_pro(app_client, db_session):
    """An empty bundle (no members yet) is 'nothing here', not 'paywalled'."""
    _mk_bundle(db_session, name="Empty", slug="empty-discover")
    db_session.commit()

    resp = app_client.get("/api/bundles/discover")
    cards = {c["slug"]: c for c in resp.json()["bundles"]}
    assert cards["empty-discover"]["requires_pro"] is False


def test_discover_disabled_member_excluded_from_the_computation(app_client, db_session):
    """A bundle whose only ACTIVE member is free, plus a disabled Pro member,
    must not be flagged — disabled members are invisible to install.sh too."""
    b = _mk_bundle(db_session, name="Disabled Pro", slug="disabled-pro-discover")
    _link(db_session, b, _mk_skill(db_session, slug="free-c-discover", tier="free"))
    _link(db_session, b, _mk_skill(db_session, slug="pro-d-discover", tier="pro"), source="disabled")
    db_session.commit()

    resp = app_client.get("/api/bundles/discover")
    cards = {c["slug"]: c for c in resp.json()["bundles"]}
    assert cards["disabled-pro-discover"]["requires_pro"] is False


# ═════════════════════════════════════════════════════════════════════════
# GET /api/bundles/public/{slug}
# ═════════════════════════════════════════════════════════════════════════


def test_public_page_flags_all_pro_bundle(app_client, db_session):
    b = _mk_bundle(db_session, name="All Pro Page", slug="all-pro-page")
    _link(db_session, b, _mk_skill(db_session, slug="pro-e-page", tier="pro"))
    db_session.commit()

    resp = app_client.get("/api/bundles/public/all-pro-page")
    assert resp.status_code == 200, resp.text
    assert resp.json()["requires_pro"] is True


def test_public_page_does_not_flag_bundle_with_a_free_member(app_client, db_session):
    b = _mk_bundle(db_session, name="Has Free Page", slug="has-free-page")
    _link(db_session, b, _mk_skill(db_session, slug="free-d-page", tier="free"))
    _link(db_session, b, _mk_skill(db_session, slug="pro-f-page", tier="pro"))
    db_session.commit()

    resp = app_client.get("/api/bundles/public/has-free-page")
    assert resp.json()["requires_pro"] is False


# ═════════════════════════════════════════════════════════════════════════
# Unit-level: the helper itself, incl. the redistributable-federated case
# ═════════════════════════════════════════════════════════════════════════


def test_helper_treats_redistributable_external_skill_as_not_pro_locked(db_session):
    """A materialized federated skill (tier='external', not in _FREE_TIERS)
    whose license permits redistribution must NOT count toward requires_pro —
    mirrors the well-known index's own _is_redistributable_external gate
    exactly (bundles0811-P1-follow-seed gate 2)."""
    from app.bundle_routes import _bundle_requires_pro
    from app.models import BundleSkill, Skill

    ext_skill = Skill(
        id=uuid.uuid4(),
        slug="ext:some-source:some-skill",
        title="External",
        tier="external",
        is_public=True,
        external_resources={"redistributable": True, "install_path": "fetch_origin"},
    )
    db_session.add(ext_skill)
    db_session.flush()
    fake_bs = BundleSkill(bundle_id=uuid.uuid4(), skill_id=ext_skill.id, source="custom-added")

    assert _bundle_requires_pro([(fake_bs, ext_skill)]) is False


def test_helper_empty_list_is_false():
    from app.bundle_routes import _bundle_requires_pro

    assert _bundle_requires_pro([]) is False
