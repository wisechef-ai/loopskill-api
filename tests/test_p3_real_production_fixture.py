"""converge_0208 P3 — fixture matching the real 13 dead rows on 2026-08-03.

This test creates a production-realistic fixture of the exact 13 rows that
exist on wisechef-hq with `/storage/skills/` paths, verifies the repair
script detects and names them, and verifies the probe catches the defects.

The fixture data is from SHARED_CONTEXT.md §1a (verified live, 2026-08-03).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models import Bundle, BundleSkill, Skill, SkillVersion
from scripts.probe_pinned_versions import (
    find_shape_defects,
    probe_all_pinned_versions,
)
from scripts.repair_dead_skill_version_paths import plan_repairs, plan_track_pin_repair


REAL_DEAD_ROWS = [
    ("client-reporter", "1.0.0"),
    ("code-reviewer", "1.0.0"),
    ("data-pipeline", "0.5.0"),
    ("email-composer", "2.0.1"),
    ("hub-search-claude-code", "1.0.0"),
    ("image-generator", "0.9.0"),
    ("llm-wiki-hermes", "2.1.0"),
    ("loopskill", "1.0.0"),
    ("plan-for-goal", "1.0.0"),
    ("ruthless-mentor", "1.0.0"),
    ("super-memory", "1.0.0"),
    ("web-scraper-pro", "1.2.0"),
    ("web-scraper-pro", "1.1.0"),
]

TORI_DECLARED = [
    ("ruthless-mentor", "1.0.0"),
    ("super-memory", "1.0.0"),
    ("llm-wiki-hermes", "2.1.0"),
    ("plan-for-goal", "1.0.0"),
    ("hub-search-claude-code", "1.0.0"),
    ("musk-5-step-algorithm", None),
    ("codex-sandbox-recovery", "1.0.2"),
    ("loopskill", "1.0.0"),
]


def make_skill(db, slug, **kwargs):
    from tests.conftest import make_skill as _make_skill

    return _make_skill(db, slug=slug, **kwargs)


def make_skill_version(db, skill, semver, tarball_path=None, **kwargs):
    v = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver=semver,
        tarball_path=tarball_path,
        **kwargs
    )
    db.add(v)
    db.flush()
    return v


def make_bundle(db, name="test-bundle", slug=None):
    b = Bundle(id=uuid4(), name=name, slug=slug)
    db.add(b)
    db.flush()
    return b


def make_bundle_skill(db, bundle, skill, pin_mode="track", pinned_version=None):
    bs = BundleSkill(
        id=uuid4(),
        bundle_id=bundle.id,
        skill_id=skill.id,
        source="local",
        pin_mode=pin_mode,
        pinned_version=pinned_version,
    )
    db.add(bs)
    db.flush()
    return bs


@pytest.fixture
def production_fixture(db_session):
    """Create the exact 13 dead rows from production (2026-08-03)."""
    skills_by_slug = {}
    for slug, semver in REAL_DEAD_ROWS:
        if slug not in skills_by_slug:
            skills_by_slug[slug] = make_skill(db_session, slug=slug)
        skill = skills_by_slug[slug]
        make_skill_version(
            db_session,
            skill,
            semver,
            tarball_path=f"/storage/skills/{slug}-{semver}.tar.gz",
        )

    # tori-core bundle with the 8 declared skills (source of the 1295 rollbacks).
    tori = make_bundle(db_session, name="tori-core", slug="tori-core")
    for skill_slug, pinned_ver in TORI_DECLARED:
        if skill_slug not in skills_by_slug:
            skill = make_skill(db_session, slug=skill_slug)
            skills_by_slug[skill_slug] = skill
        else:
            skill = skills_by_slug[skill_slug]
        make_bundle_skill(db_session, tori, skill, pin_mode="track", pinned_version=pinned_ver)

    return {"skills": skills_by_slug, "tori_bundle": tori}


def test_repair_detects_all_13_dead_rows(db_session, production_fixture):
    """The repair script detects all 13 dead /storage/skills/ paths."""
    plans = plan_repairs(db_session)

    # All 13 rows are dead (no canonical /var/lib/recipes-skills artifacts exist in test).
    assert len(plans) == 13
    slugs = {p["slug"] for p in plans}
    assert slugs == {slug for slug, _ in REAL_DEAD_ROWS}

    # Most should mark unresolvable (no newer version exists in this fixture).
    actions = {p["action"] for p in plans}
    assert actions == {"mark_unresolvable"}


def test_probe_catches_all_tori_pinned_failures(db_session, production_fixture):
    """The probe catches that tori-core's pinned versions are unresolvable."""
    tori = production_fixture["tori_bundle"]
    failures = probe_all_pinned_versions(db_session)

    # The 7 entries with non-NULL pinned_version (all now unresolvable).
    assert len(failures) == 7
    failing_slugs = {f["skill_slug"] for f in failures}
    expected = {slug for slug, pin in TORI_DECLARED if pin is not None}
    assert failing_slugs == expected


def test_probe_warns_on_tori_shape_defects(db_session, production_fixture):
    """The probe flags tori-core's pin_mode='track' + stale pin as a shape defect."""
    defects = find_shape_defects(db_session)

    # 7 of 8 declared skills have stale pins (1 is NULL).
    assert len(defects) == 7
    # All defects are from tori-core.
    assert all(d["bundle_slug"] == "tori-core" for d in defects)


def test_track_pin_repair_clears_all_tori_stale_pins(db_session, production_fixture):
    """Track-pin repair clears the 7 stale pins in tori-core."""
    tori = production_fixture["tori_bundle"]
    plans = plan_track_pin_repair(db_session, bundle_slug="tori-core")

    assert len(plans) == 7
    # After repair, no more defects.
    from scripts.repair_dead_skill_version_paths import apply_track_pin_repair

    apply_track_pin_repair(db_session, plans)
    db_session.flush()

    remaining = plan_track_pin_repair(db_session, bundle_slug="tori-core")
    assert remaining == []
