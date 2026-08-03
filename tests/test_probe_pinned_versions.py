"""Tests for scripts/probe_pinned_versions.py (converge_0208 P3).

RED-proof: deliberately breaks rows and confirms the probe detects them.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models import Bundle, BundleSkill, Skill, SkillVersion
from scripts.probe_pinned_versions import (
    find_shape_defects,
    probe_all_pinned_versions,
    probe_bundle_pins,
    resolve_pinned_version,
)


def make_skill(db, slug="test-skill", **kwargs):
    from tests.conftest import make_skill as _make_skill

    return _make_skill(db, slug=slug, **kwargs)


def make_skill_version(db, skill, semver, tarball_path=None, resolution_status="ok", **kwargs):
    v = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver=semver,
        tarball_path=tarball_path,
        resolution_status=resolution_status,
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


# ── Healthy case: pinned version resolves ───────────────────────────────


def test_probe_passes_when_pinned_version_resolves(db_session, tmp_path):
    skill = make_skill(db_session, slug="ruthless-mentor")
    bundle = make_bundle(db_session, name="tori-core", slug="tori-core")
    make_bundle_skill(db_session, bundle, skill, pin_mode="pin", pinned_version="1.0.1")

    tarball = tmp_path / "ruthless-mentor" / "1.0.1.tar.gz"
    tarball.parent.mkdir(parents=True)
    tarball.write_bytes(b"bytes")
    make_skill_version(db_session, skill, "1.0.1", str(tarball))

    failures = probe_bundle_pins(db_session, bundle.id)
    assert failures == []


# ── RED-proof case 1: pinned version row doesn't exist ────────────────


def test_probe_fails_when_pinned_version_row_missing(db_session):
    """RED-proof: no SkillVersion row exists for the pinned version."""
    skill = make_skill(db_session, slug="ruthless-mentor")
    bundle = make_bundle(db_session, name="tori-core", slug="tori-core")
    make_bundle_skill(db_session, bundle, skill, pin_mode="pin", pinned_version="1.0.0")
    # Deliberately skip creating the SkillVersion row for 1.0.0

    failures = probe_bundle_pins(db_session, bundle.id)

    assert len(failures) == 1
    assert failures[0]["skill_slug"] == "ruthless-mentor"
    assert failures[0]["pinned_version"] == "1.0.0"
    assert "no SkillVersion row" in failures[0]["reason"]


# ── RED-proof case 2: tarball_path is dead (file doesn't exist) ────────


def test_probe_fails_when_tarball_path_dead(db_session):
    """RED-proof: the tarball_path points to a file that doesn't exist."""
    skill = make_skill(db_session, slug="ruthless-mentor")
    bundle = make_bundle(db_session, name="tori-core", slug="tori-core")
    make_bundle_skill(db_session, bundle, skill, pin_mode="pin", pinned_version="1.0.0")
    make_skill_version(
        db_session, skill, "1.0.0", tarball_path="/storage/skills/ruthless-mentor-1.0.0.tar.gz"
    )

    failures = probe_bundle_pins(db_session, bundle.id)

    assert len(failures) == 1
    assert failures[0]["skill_slug"] == "ruthless-mentor"
    assert "not found at /storage/skills" in failures[0]["reason"]


# ── RED-proof case 3: row marked unresolvable ────────────────────────


def test_probe_fails_when_version_marked_unresolvable(db_session, tmp_path):
    """RED-proof: row exists and could have bytes, but is explicitly marked unresolvable."""
    skill = make_skill(db_session, slug="ruthless-mentor")
    bundle = make_bundle(db_session, name="tori-core", slug="tori-core")
    make_bundle_skill(db_session, bundle, skill, pin_mode="pin", pinned_version="1.0.0")

    tarball = tmp_path / "ruthless-mentor" / "1.0.0.tar.gz"
    tarball.parent.mkdir(parents=True)
    tarball.write_bytes(b"bytes")
    make_skill_version(
        db_session, skill, "1.0.0", tarball_path=str(tarball), resolution_status="unresolvable"
    )

    failures = probe_bundle_pins(db_session, bundle.id)

    assert len(failures) == 1
    assert "marked unresolvable" in failures[0]["reason"]


# ── RED-proof case 4: tarball_path is NULL ───────────────────────────


def test_probe_fails_when_tarball_path_null(db_session):
    """RED-proof: row exists but has no tarball_path set."""
    skill = make_skill(db_session, slug="super-memory")
    bundle = make_bundle(db_session, name="tori-core", slug="tori-core")
    make_bundle_skill(db_session, bundle, skill, pin_mode="pin", pinned_version="1.0.0")
    make_skill_version(db_session, skill, "1.0.0", tarball_path=None)

    failures = probe_bundle_pins(db_session, bundle.id)

    assert len(failures) == 1
    assert "no tarball_path" in failures[0]["reason"]


# ── Global probe: aggregates failures across all bundles ───────────────


def test_probe_all_pinned_versions_aggregates_failures(db_session, tmp_path):
    """Aggregates failures from multiple bundles."""
    skill_a = make_skill(db_session, slug="ruthless-mentor")
    skill_b = make_skill(db_session, slug="super-memory")

    bundle_a = make_bundle(db_session, name="tori-core", slug="tori-core")
    bundle_b = make_bundle(db_session, name="other-bundle", slug="other-bundle")

    make_bundle_skill(db_session, bundle_a, skill_a, pin_mode="pin", pinned_version="1.0.0")
    make_bundle_skill(db_session, bundle_b, skill_b, pin_mode="pin", pinned_version="1.0.0")

    # Both are dead — no version rows exist.
    all_failures = probe_all_pinned_versions(db_session)

    assert len(all_failures) == 2
    slugs = {f["skill_slug"] for f in all_failures}
    assert slugs == {"ruthless-mentor", "super-memory"}


# ── Bundles with no pins are ignored ─────────────────────────────────


def test_probe_ignores_bundles_with_no_pins(db_session):
    skill = make_skill(db_session, slug="loopskill")
    bundle = make_bundle(db_session, name="my-bundle", slug="my-bundle")
    make_bundle_skill(db_session, bundle, skill, pin_mode="track", pinned_version=None)

    failures = probe_bundle_pins(db_session, bundle.id)
    assert failures == []

    all_failures = probe_all_pinned_versions(db_session)
    assert all_failures == []


# ── Shape defect detection: track + non-NULL pin ──────────────────────


def test_shape_defect_track_with_stale_pin(db_session):
    """Shape defect: pin_mode='track' should not carry a pinned_version."""
    skill = make_skill(db_session, slug="ruthless-mentor")
    bundle = make_bundle(db_session, name="tori-core", slug="tori-core")
    make_bundle_skill(db_session, bundle, skill, pin_mode="track", pinned_version="1.0.0")

    defects = find_shape_defects(db_session)

    assert len(defects) == 1
    assert defects[0]["skill_slug"] == "ruthless-mentor"
    assert defects[0]["pinned_version"] == "1.0.0"


def test_no_shape_defect_when_track_has_no_pin(db_session):
    skill = make_skill(db_session, slug="ruthless-mentor")
    bundle = make_bundle(db_session, name="tori-core", slug="tori-core")
    make_bundle_skill(db_session, bundle, skill, pin_mode="track", pinned_version=None)

    defects = find_shape_defects(db_session)
    assert defects == []


def test_no_shape_defect_for_deliberate_pin(db_session):
    """pin_mode='pin' is supposed to have a pinned_version."""
    skill = make_skill(db_session, slug="web-scraper-pro")
    bundle = make_bundle(db_session, name="some-bundle", slug="some-bundle")
    make_bundle_skill(db_session, bundle, skill, pin_mode="pin", pinned_version="1.1.0")

    defects = find_shape_defects(db_session)
    assert defects == []


def test_real_torichain_shape_defect_all_eight(db_session):
    """Real-world scenario: tori-core declares 8 skills with stale pins."""
    tori = make_bundle(db_session, name="tori-core", slug="tori-core")
    declared = [
        ("ruthless-mentor", "1.0.0"),
        ("super-memory", "1.0.0"),
        ("llm-wiki-hermes", "2.1.0"),
        ("plan-for-goal", "1.0.0"),
        ("hub-search-claude-code", "1.0.0"),
        ("musk-5-step-algorithm", None),
        ("codex-sandbox-recovery", "1.0.2"),
        ("loopskill", "1.0.0"),
    ]
    for slug, pin in declared:
        skill = make_skill(db_session, slug=slug)
        make_bundle_skill(db_session, tori, skill, pin_mode="track", pinned_version=pin)

    defects = find_shape_defects(db_session)

    # 7 of 8 have non-NULL pins; musk-5-step-algorithm has NULL (no defect).
    assert len(defects) == 7
    slugs = {d["skill_slug"] for d in defects}
    assert "musk-5-step-algorithm" not in slugs
