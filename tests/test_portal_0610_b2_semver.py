"""portal_0610 B2 — semantic semver comparison (§6.6).

Pins the fix for the lexicographic-max bug: ``max("1.9.0","1.10.0")`` must be
"1.10.0", not "1.9.0". Tests the pure helpers AND the per-skill DB resolver.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.services.semver import (
    latest_semver_for_skills,
    latest_version_row_for_skill,
    max_semver,
    semver_key,
)


def test_semver_key_double_digit_minor_orders_correctly():
    # The exact bug: lexicographic "1.10.0" < "1.9.0" is WRONG.
    assert semver_key("1.10.0") > semver_key("1.9.0")
    assert semver_key("1.2.10") > semver_key("1.2.9")
    assert semver_key("2.0.0") > semver_key("1.99.99")


def test_semver_key_tolerates_v_prefix_and_build_suffix():
    assert semver_key("v1.2.3") == semver_key("1.2.3")
    assert semver_key("1.2.3+build7") == semver_key("1.2.3")


def test_semver_key_prerelease_sorts_before_release():
    assert semver_key("1.2.0-rc1") < semver_key("1.2.0")
    assert semver_key("1.2.0-rc1") < semver_key("1.2.0+build")


def test_semver_key_unparseable_floors():
    assert semver_key("garbage") == (-1, 0, 0, 0)
    assert semver_key(None) == (-1, 0, 0, 0)
    # a real version always beats garbage
    assert semver_key("0.0.1") > semver_key("not-a-version")


def test_max_semver():
    assert max_semver(["1.9.0", "1.10.0", "1.2.0"]) == "1.10.0"
    assert max_semver(["2.0.0", "10.0.0", "1.0.0"]) == "10.0.0"
    assert max_semver([]) is None
    assert max_semver([None, None]) is None
    assert max_semver([None, "1.0.0"]) == "1.0.0"


def _mk_skill_with_versions(db, slug, semvers):
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
    for i, sv in enumerate(semvers):
        db.add(
            SkillVersion(
                id=uuid.uuid4(),
                skill_id=sk.id,
                semver=sv,
                tarball_size_bytes=1,
                checksum_sha256="x" * 8,
                # created_at intentionally NOT monotonic w.r.t. semver, to prove
                # selection is by semver not insert/created order.
                created_at=datetime.now(timezone.utc),
            )
        )
    db.flush()
    return sk


def test_latest_semver_for_skills_picks_semantic_max(db_session):
    sk = _mk_skill_with_versions(db_session, "b2-probe", ["1.9.0", "1.10.0", "1.2.0"])
    out = latest_semver_for_skills(db_session, [sk.id])
    assert out[sk.id] == "1.10.0", "B2: must pick semantic max, not lexicographic"


def test_latest_semver_for_skills_empty_and_missing(db_session):
    assert latest_semver_for_skills(db_session, []) == {}
    # a skill id with no versions is absent from the result
    missing = uuid.uuid4()
    assert missing not in latest_semver_for_skills(db_session, [missing])


def test_latest_version_row_for_skill_semantic(db_session):
    sk = _mk_skill_with_versions(db_session, "b2-row-probe", ["1.9.0", "1.10.0"])
    row = latest_version_row_for_skill(db_session, sk.id)
    assert row is not None
    assert row.semver == "1.10.0"


def test_latest_version_row_promoted_only(db_session):
    from app.models import SkillVersion

    sk = _mk_skill_with_versions(db_session, "b2-promote-probe", ["1.9.0", "1.10.0"])
    # Promote ONLY the 1.9.0 version to stable.
    v190 = (
        db_session.query(SkillVersion)
        .filter(SkillVersion.skill_id == sk.id, SkillVersion.semver == "1.9.0")
        .first()
    )
    v190.promoted_to_stable_at = datetime.now(timezone.utc)
    db_session.flush()

    # stable channel sees only the promoted one (1.9.0), even though 1.10.0 is newer.
    stable_row = latest_version_row_for_skill(db_session, sk.id, promoted_only=True)
    assert stable_row is not None and stable_row.semver == "1.9.0"
    # canary (all) still sees the semantic max.
    canary_row = latest_version_row_for_skill(db_session, sk.id, promoted_only=False)
    assert canary_row.semver == "1.10.0"
