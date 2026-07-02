"""Phase 0 (loopskill_activate_0701): the reconcile diff must carry signed
``tarball_url``s the client fetcher can pull.

Live-prod finding (2026-07-02, first REAL reconcile ever run): the server's
``compute_reconcile_plan`` emitted add/update rows with only
{slug, version, checksum_sha256}, while ``reconcile_fetch.make_fetcher``
requires a ``tarball_url`` per entry — so every real apply failed with
"no tarball_url in reconcile diff" and auto-rolled-back. The engine and the
shipped client had never been run together.

Contract pinned here: every ``add``/``update``/``drift`` row returned by the
reconcile ROUTE carries a signed one-shot ``tarball_url`` that verifies against
``install_routes._download``'s salt chain.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import Bundle, BundleSkill, Skill, SkillVersion, User
from app.services.reconcile import recipes_reconcile
from app.auth_ctx import AuthContext


@pytest.fixture()
def owner(db_session: Session) -> User:
    u = User(display_name="reconcile-owner", email=f"{uuid4().hex[:8]}@t.local")
    db_session.add(u)
    db_session.commit()
    return u


@pytest.fixture()
def bundle_with_skill(db_session: Session, owner: User) -> tuple[Bundle, Skill]:
    skill = Skill(
        slug=f"tarball-contract-{uuid4().hex[:6]}",
        title="Tarball Contract Skill",
        description="fixture",
        category="testing",
        tier="free",
        is_public=False,
    )
    db_session.add(skill)
    db_session.commit()
    ver = SkillVersion(
        skill_id=skill.id,
        semver="1.0.0",
        tarball_path=f"/tmp/{skill.slug}/1.0.0.tar.gz",
        checksum_sha256=hashlib.sha256(b"x").hexdigest(),
    )
    db_session.add(ver)
    cb = Bundle(name="tarball-contract-bundle", bundle_owner=owner.id)
    db_session.add(cb)
    db_session.commit()
    db_session.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
    db_session.commit()
    return cb, skill


def test_reconcile_add_rows_carry_signed_tarball_url(
    db_session: Session, owner: User, bundle_with_skill: tuple[Bundle, Skill]
) -> None:
    cb, skill = bundle_with_skill
    ctx = AuthContext(scope="user", user_id=owner.id)
    result = recipes_reconcile(
        db_session, cookbook_id=str(cb.id), local=[], dry_run=True, ctx=ctx
    )
    assert "error" not in result, result
    adds = result["diff"]["add"]
    assert len(adds) == 1
    row = adds[0]
    assert row["slug"] == skill.slug
    assert "tarball_url" in row, "reconcile diff rows MUST carry tarball_url (Phase 0 contract)"
    assert "/api/skills/_download?token=" in row["tarball_url"]


def test_reconcile_update_rows_carry_signed_tarball_url(
    db_session: Session, owner: User, bundle_with_skill: tuple[Bundle, Skill]
) -> None:
    cb, skill = bundle_with_skill
    ctx = AuthContext(scope="user", user_id=owner.id)
    local = [{"slug": skill.slug, "pinned_version": "0.9.0", "sha256": None}]
    result = recipes_reconcile(
        db_session, cookbook_id=str(cb.id), local=local, dry_run=True, ctx=ctx
    )
    updates = result["diff"]["update"]
    assert len(updates) == 1
    assert "tarball_url" in updates[0]


def test_tarball_url_token_verifies_against_download_salt_chain(
    db_session: Session, owner: User, bundle_with_skill: tuple[Bundle, Skill]
) -> None:
    """The signed token must verify with install_routes' verifier (salt parity)."""
    from urllib.parse import parse_qs, urlparse

    from app.config import settings
    from app.install_routes import _verify_signed_token

    cb, skill = bundle_with_skill
    ctx = AuthContext(scope="user", user_id=owner.id)
    result = recipes_reconcile(
        db_session, cookbook_id=str(cb.id), local=[], dry_run=True, ctx=ctx
    )
    url = result["diff"]["add"][0]["tarball_url"]
    token = parse_qs(urlparse(url).query)["token"][0]
    payload = _verify_signed_token(token, secret=settings.SIGNING_SECRET)
    assert payload["slug"] == skill.slug
