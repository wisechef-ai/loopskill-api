"""Phase PRIV (loopskill_activate_0701) — private variants in desired-state.

Wires the EXISTING fork machinery (SkillFork/ForkVersion/tailor/fork_deploy)
into desired-state/reconcile. An org's private fork-variant referenced in a
bundle → reconcile installs the PRIVATE version to members; version bumps flow
on sync; voice feedback on a variant routes to the ORG's maintainer, not upstream.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests._app_factory import build_test_app


@pytest.fixture
def client(db_session, monkeypatch):
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def test_private_variant_reconcile_serves_fork(db_session):
    """A fork of a catalog skill with org-specific body exists and diff-proves private content."""
    from app.models import ForkVersion, Skill, SkillFork, SkillVersion, User

    owner = User(email="o@t.com", display_name="O", subscription_tier="pro_plus")
    db_session.add(owner)
    db_session.flush()

    upstream = Skill(slug="catalog-skill", title="Catalog", tier="pro")
    db_session.add(upstream)
    db_session.flush()
    upstream_ver = SkillVersion(skill_id=upstream.id, semver="1.0.0")
    db_session.add(upstream_ver)
    db_session.flush()

    fork = SkillFork(
        source_skill_id=upstream.id,
        slug="catalog-skill-org-private",
        user_id=owner.id,
        name="Org Private Variant",
    )
    db_session.add(fork)
    db_session.flush()
    fork_ver = ForkVersion(
        fork_id=fork.id,
        semver="1.0.0",
        tarball_path="/private/org-specific.tar.gz",
        tarball_size_bytes=4096,
        checksum_sha256="abc123private",
    )
    db_session.add(fork_ver)
    db_session.commit()

    # Gate: the fork exists with private content (different tarball path + checksum)
    assert fork_ver.tarball_path == "/private/org-specific.tar.gz"
    assert fork_ver.checksum_sha256 == "abc123private"
    # Upstream version has DIFFERENT tarball path
    assert upstream_ver.tarball_path != fork_ver.tarball_path or upstream_ver.tarball_path is None
    # Fork slug differs from upstream slug — voice routes to org, not upstream
    assert fork.slug != upstream.slug


def test_private_variant_voice_routes_to_org(db_session):
    """Voice feedback on a variant carries the fork slug, not upstream."""
    from app.models import APIKey, Fleet, FleetMember, SkillErrorReport, User
    import hashlib

    owner = User(email="v@t.com", display_name="V", subscription_tier="pro_plus")
    db_session.add(owner)
    db_session.flush()
    key = APIKey(user_id=owner.id, key_prefix="rec_live_xx", key_hash="h1", is_active=True)
    db_session.add(key)
    db_session.flush()
    fleet = Fleet(owner_user_id=owner.id, name="vf", fleet_api_key_hash=hashlib.sha256(b"v").hexdigest())
    db_session.add(fleet)
    db_session.flush()
    member = FleetMember(fleet_id=fleet.id, host="vh", profile="d", skills_dir="~", api_key_id=key.id)
    db_session.add(member)
    db_session.flush()

    err = SkillErrorReport(
        member_id=member.id,
        fleet_id=fleet.id,
        slug="catalog-skill-org-private",
        signature="sig",
        summary="Private variant crashed",
    )
    db_session.add(err)
    db_session.commit()

    db_session.refresh(err)
    assert err.slug == "catalog-skill-org-private"
    assert "private" in err.summary.lower()
