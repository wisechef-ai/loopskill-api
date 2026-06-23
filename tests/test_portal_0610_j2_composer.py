"""portal_0610 J2 — Composer backend mutations (visibility, version-pin, reorder).

The Composer (L3) is the hero authed surface. It needs three cookbook mutations
beyond add/remove, which this suite pins:

  - PATCH /api/cookbooks/{id}/visibility       — flip public/private (L3)
  - PATCH /api/cookbooks/{id}/skills/{slug}/pin — version-pin, CURATED-ONLY (L5)
  - PATCH /api/cookbooks/{id}/reorder          — persist Composer order (L3)

Plus the install_order column makes _skills_for emit in Composer order.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_user(db, *, tier="pro", status="active"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="composer-owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status=status,
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user):
    from app.models import APIKey

    raw = f"rec_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:8],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name="j2",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _mk_cookbook(db, owner, *, visibility="private"):
    from app.models import Cookbook

    cb = Cookbook(id=uuid.uuid4(), name="deck", bundle_owner=owner.id, visibility=visibility)
    db.add(cb)
    db.flush()
    return cb


def _mk_skill_with_versions(db, slug, semvers=("1.0.0",)):
    from app.models import Skill, SkillVersion

    sk = Skill(
        id=uuid.uuid4(),
        slug=slug,
        title=slug,
        description="t",
        tier="free",
        is_public=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(sk)
    db.flush()
    for sv in semvers:
        db.add(
            SkillVersion(
                id=uuid.uuid4(),
                skill_id=sk.id,
                semver=sv,
                tarball_size_bytes=1,
                checksum_sha256="x" * 8,
                created_at=datetime.now(timezone.utc),
            )
        )
    db.flush()
    return sk


def _add(db, cb, skill, *, source="custom-added", order=100):
    from app.models import CookbookSkill

    db.add(CookbookSkill(bundle_id=cb.id, skill_id=skill.id, source=source, install_order=order))
    db.flush()


# ── visibility ─────────────────────────────────────────────────────────────


def test_visibility_toggle(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner, visibility="private")

    r = middleware_client.patch(
        f"/api/cookbooks/{cb.id}/visibility", headers={"x-api-key": key}, json={"visibility": "public"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["visibility"] == "public"

    r2 = middleware_client.patch(
        f"/api/cookbooks/{cb.id}/visibility", headers={"x-api-key": key}, json={"visibility": "bogus"}
    )
    assert r2.status_code == 422


def test_visibility_non_owner_404(middleware_client, db_session):
    owner = _mk_user(db_session)
    other = _mk_user(db_session)
    other_key = _mk_key(db_session, other)
    cb = _mk_cookbook(db_session, owner)
    r = middleware_client.patch(
        f"/api/cookbooks/{cb.id}/visibility", headers={"x-api-key": other_key}, json={"visibility": "public"}
    )
    assert r.status_code in (403, 404)


# ── version-pin (L5, curated-only) ──────────────────────────────────────────


def test_pin_curated_skill(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    sk = _mk_skill_with_versions(db_session, "j2-pinnable", ("1.0.0", "1.1.0"))
    _add(db_session, cb, sk)

    r = middleware_client.patch(
        f"/api/cookbooks/{cb.id}/skills/j2-pinnable/pin",
        headers={"x-api-key": key},
        json={"pinned_version": "1.0.0"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["pinned_version"] == "1.0.0"
    assert r.json()["pinned"] is True

    # clearing the pin → always-latest
    r2 = middleware_client.patch(
        f"/api/cookbooks/{cb.id}/skills/j2-pinnable/pin",
        headers={"x-api-key": key},
        json={"pinned_version": None},
    )
    assert r2.status_code == 200
    assert r2.json()["pinned"] is False


def test_pin_nonexistent_version_404(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    sk = _mk_skill_with_versions(db_session, "j2-badpin", ("1.0.0",))
    _add(db_session, cb, sk)

    r = middleware_client.patch(
        f"/api/cookbooks/{cb.id}/skills/j2-badpin/pin",
        headers={"x-api-key": key},
        json={"pinned_version": "9.9.9"},
    )
    assert r.status_code == 404


def test_pin_external_skill_rejected(middleware_client, db_session):
    """L5: federation skills have no version contract — pinning must 422."""
    from app.models import Skill, CookbookSkill

    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    # An external/materialized skill: skill_variant='external' marks it external.
    ext = Skill(
        id=uuid.uuid4(),
        slug="ext:lobehub:persona",
        title="ext persona",
        description="t",
        tier="free",
        is_public=False,
        created_at=datetime.now(timezone.utc),
        skill_variant="external",
    )
    db_session.add(ext)
    db_session.flush()
    db_session.add(CookbookSkill(bundle_id=cb.id, skill_id=ext.id, source="custom-added"))
    db_session.flush()

    r = middleware_client.patch(
        f"/api/cookbooks/{cb.id}/skills/ext:lobehub:persona/pin",
        headers={"x-api-key": key},
        json={"pinned_version": "1.0.0"},
    )
    assert r.status_code == 422
    assert "external" in r.json()["detail"].lower()


# ── reorder (L3) ─────────────────────────────────────────────────────────────


def test_reorder_persists_install_order(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    a = _mk_skill_with_versions(db_session, "j2-a")
    b = _mk_skill_with_versions(db_session, "j2-b")
    c = _mk_skill_with_versions(db_session, "j2-c")
    _add(db_session, cb, a, order=10)
    _add(db_session, cb, b, order=20)
    _add(db_session, cb, c, order=30)

    # Reverse the order
    r = middleware_client.patch(
        f"/api/cookbooks/{cb.id}/reorder",
        headers={"x-api-key": key},
        json={"order": ["j2-c", "j2-b", "j2-a"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["order"][:3] == ["j2-c", "j2-b", "j2-a"]

    # Verify _skills_for now emits in the new order.
    from app.cookbook_routes import _skills_for

    rows = _skills_for(db_session, cb.id, include_disabled=False)
    slugs = [skill.slug for _cs, skill in rows]
    assert slugs == ["j2-c", "j2-b", "j2-a"]
