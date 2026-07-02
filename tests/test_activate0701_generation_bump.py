"""Phase 0 (loopskill_activate_0701): publishing a new skill version must
advance the generation of every bundle that declares the skill.

Live-prod finding #4 (2026-07-02): the reconcile 304 fast-path compares
If-None-Match against ``Bundle.updated_at`` — but publishing a new
``SkillVersion`` never touched the bundle row. Result: an agent whose
lockfile generation matched got 304 FOREVER and never saw any published
update. The diff engine itself was correct (bypassing the ETag showed the
1.0.0→1.0.1 update) — the cheap path made updates invisible. This breaks the
entire evergreen premise for every polling agent.

Contract pinned here: after a publish, every bundle declaring the skill has
a NEWER generation token, so the next conditional poll gets 200 + diff.
"""

from __future__ import annotations

import hashlib

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import Bundle, BundleSkill, Skill, SkillVersion, User
from app.publisher_routes import _bump_declaring_bundles


@pytest.fixture()
def owner(db_session: Session) -> User:
    u = User(display_name="gen-owner", email=f"{uuid4().hex[:8]}@t.local")
    db_session.add(u)
    db_session.commit()
    return u


def _mk_skill_bundle(db: Session, owner: User, declare: bool = True) -> tuple[Skill, Bundle]:
    skill = Skill(
        slug=f"gen-bump-{uuid4().hex[:6]}",
        title="Gen Bump",
        description="fixture",
        category="testing",
        tier="free",
        is_public=False,
    )
    db.add(skill)
    db.commit()
    db.add(
        SkillVersion(
            skill_id=skill.id,
            semver="1.0.0",
            tarball_path=f"/tmp/{skill.slug}/1.0.0.tar.gz",
            checksum_sha256=hashlib.sha256(b"v1").hexdigest(),
        )
    )
    cb = Bundle(name=f"gen-bundle-{uuid4().hex[:6]}", bundle_owner=owner.id)
    db.add(cb)
    db.commit()
    if declare:
        db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
        db.commit()
    return skill, cb


def test_publish_bumps_declaring_bundle_generation(db_session: Session, owner: User) -> None:
    skill, cb = _mk_skill_bundle(db_session, owner)
    # Backdate the generation (SQLite func.now() is second-resolution — a
    # sleep-based test would be flaky; an explicit past timestamp is not).
    from datetime import datetime, timedelta

    before = datetime.utcnow() - timedelta(hours=1)
    db_session.query(Bundle).filter(Bundle.id == cb.id).update(
        {"updated_at": before}, synchronize_session=False
    )
    db_session.commit()

    _bump_declaring_bundles(db_session, skill.id)
    db_session.commit()
    db_session.refresh(cb)

    assert cb.updated_at is not None
    assert cb.updated_at > before, (
        "generation must advance so the 304 fast-path breaks and the agent "
        "sees the new version on its next poll"
    )


def test_publish_does_not_touch_unrelated_bundles(db_session: Session, owner: User) -> None:
    skill, _cb = _mk_skill_bundle(db_session, owner)
    _other_skill, other_cb = _mk_skill_bundle(db_session, owner)
    unrelated_before = other_cb.updated_at

    _bump_declaring_bundles(db_session, skill.id)
    db_session.commit()
    db_session.refresh(other_cb)

    assert other_cb.updated_at == unrelated_before, (
        "bundles that do not declare the skill must keep their generation "
        "(no spurious 200s / cache invalidations fleet-wide)"
    )


def test_disabled_declarations_do_not_bump(db_session: Session, owner: User) -> None:
    skill, cb = _mk_skill_bundle(db_session, owner, declare=False)
    db_session.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="disabled"))
    db_session.commit()
    before = cb.updated_at

    _bump_declaring_bundles(db_session, skill.id)
    db_session.commit()
    db_session.refresh(cb)

    assert cb.updated_at == before, "disabled (undeclared) rows are not desired-state"
