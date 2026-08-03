"""Tests for scripts/repair_dead_skill_version_paths.py (converge_0208 P3).

Covers the three resolution branches for dead `skill_versions.tarball_path`
rows (repoint / mark-unresolvable-superseded / mark-unresolvable-gone), the
"never repoint at a different version's bytes" invariant, and the separate
stale `pin_mode='track'` + non-NULL `pinned_version` repair.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models import Bundle, BundleSkill, Skill, SkillVersion
from scripts.repair_dead_skill_version_paths import (
    apply_repairs,
    apply_track_pin_repair,
    plan_repairs,
    plan_track_pin_repair,
)


def make_skill_version(db, skill, semver, tarball_path, **kwargs):
    v = SkillVersion(id=uuid4(), skill_id=skill.id, semver=semver, tarball_path=tarball_path, **kwargs)
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


# ── Branch 1: repoint to the canonical path ─────────────────────────────


def test_repoint_when_canonical_artifact_exists(db_session, tmp_path):
    from tests.conftest import make_skill

    skill = make_skill(db_session, slug="ruthless-mentor")
    canonical_dir = tmp_path / "ruthless-mentor"
    canonical_dir.mkdir()
    canonical_file = canonical_dir / "1.0.0.tar.gz"
    canonical_file.write_bytes(b"the real bytes")

    make_skill_version(db_session, skill, "1.0.0", "/storage/skills/ruthless-mentor-1.0.0.tar.gz")

    plans = plan_repairs(db_session, artifact_root=tmp_path)

    assert len(plans) == 1
    plan = plans[0]
    assert plan["slug"] == "ruthless-mentor"
    assert plan["semver"] == "1.0.0"
    assert plan["action"] == "repoint"
    assert plan["new_path"] == str(canonical_file)
    assert plan["new_status"] == "ok"

    apply_repairs(db_session, plans)
    db_session.flush()
    row = db_session.query(SkillVersion).filter(SkillVersion.semver == "1.0.0").first()
    assert row.tarball_path == str(canonical_file)
    assert row.resolution_status == "ok"


# ── Branch 2: mark unresolvable — a newer version resolves ──────────────


def test_mark_unresolvable_when_newer_version_resolves(db_session, tmp_path):
    from tests.conftest import make_skill

    skill = make_skill(db_session, slug="ruthless-mentor")

    # Newer version 1.0.1 resolves at the canonical path.
    newer_dir = tmp_path / "ruthless-mentor"
    newer_dir.mkdir()
    newer_file = newer_dir / "1.0.1.tar.gz"
    newer_file.write_bytes(b"newer bytes")
    make_skill_version(db_session, skill, "1.0.1", str(newer_file))

    # Old 1.0.0 is dead — no canonical file for 1.0.0.
    old = make_skill_version(db_session, skill, "1.0.0", "/storage/skills/ruthless-mentor-1.0.0.tar.gz")

    plans = plan_repairs(db_session, artifact_root=tmp_path)

    assert len(plans) == 1
    plan = plans[0]
    assert plan["semver"] == "1.0.0"
    assert plan["action"] == "mark_unresolvable"
    assert plan["new_status"] == "unresolvable"
    assert "newer version" in plan["reason"]
    # The old path is NEVER repointed at the newer version's bytes.
    assert plan["new_path"] != str(newer_file)

    apply_repairs(db_session, plans)
    db_session.flush()
    db_session.refresh(old)
    assert old.resolution_status == "unresolvable"
    # Critically: tarball_path is untouched — never silently repointed at 1.0.1's bytes.
    assert old.tarball_path == "/storage/skills/ruthless-mentor-1.0.0.tar.gz"
    assert old.resolution_note is not None


# ── Branch 3: mark unresolvable — nothing resolves anywhere ──────────────


def test_mark_unresolvable_when_nothing_resolves(db_session, tmp_path):
    from tests.conftest import make_skill

    skill = make_skill(db_session, slug="client-reporter")
    v = make_skill_version(db_session, skill, "1.0.0", "/storage/skills/client-reporter-1.0.0.tar.gz")

    plans = plan_repairs(db_session, artifact_root=tmp_path)

    assert len(plans) == 1
    plan = plans[0]
    assert plan["action"] == "mark_unresolvable"
    assert "no artifact found anywhere" in plan["reason"]

    apply_repairs(db_session, plans)
    db_session.flush()
    db_session.refresh(v)
    assert v.resolution_status == "unresolvable"


# ── Rows that already resolve are left alone ─────────────────────────────


def test_healthy_row_is_not_touched(db_session, tmp_path):
    from tests.conftest import make_skill

    skill = make_skill(db_session, slug="loopskill")
    live_file = tmp_path / "already-fine.tar.gz"
    live_file.write_bytes(b"bytes")
    make_skill_version(db_session, skill, "1.0.0", str(live_file))

    plans = plan_repairs(db_session, artifact_root=tmp_path)
    assert plans == []


# ── Never fabricates a tarball ───────────────────────────────────────────


def test_no_tarball_is_ever_fabricated(db_session, tmp_path):
    """apply_repairs never writes bytes to disk — only DB state changes."""
    from tests.conftest import make_skill

    skill = make_skill(db_session, slug="data-pipeline")
    make_skill_version(db_session, skill, "0.5.0", "/storage/skills/data-pipeline-0.5.0.tar.gz")

    plans = plan_repairs(db_session, artifact_root=tmp_path)
    apply_repairs(db_session, plans)

    # No file was ever created anywhere under tmp_path.
    assert list(tmp_path.rglob("*.tar.gz")) == []


# ── Stale track-pin repair ───────────────────────────────────────────────


def test_track_pin_with_stale_pinned_version_is_planned(db_session):
    from tests.conftest import make_skill

    skill = make_skill(db_session, slug="ruthless-mentor")
    bundle = make_bundle(db_session, name="tori-core", slug="tori-core")
    bs = make_bundle_skill(db_session, bundle, skill, pin_mode="track", pinned_version="1.0.0")

    plans = plan_track_pin_repair(db_session)

    assert len(plans) == 1
    assert plans[0]["bundle_skill_id"] == bs.id
    assert plans[0]["skill_slug"] == "ruthless-mentor"
    assert plans[0]["old_pinned_version"] == "1.0.0"

    apply_track_pin_repair(db_session, plans)
    db_session.flush()
    db_session.refresh(bs)
    assert bs.pinned_version is None
    assert bs.pin_mode == "track"  # pin_mode itself is untouched


def test_pin_mode_pin_rows_are_never_touched(db_session):
    """A deliberate pin (pin_mode='pin') is exactly the intended behavior — leave it."""
    from tests.conftest import make_skill

    skill = make_skill(db_session, slug="web-scraper-pro")
    bundle = make_bundle(db_session, name="some-bundle")
    make_bundle_skill(db_session, bundle, skill, pin_mode="pin", pinned_version="1.1.0")

    plans = plan_track_pin_repair(db_session)
    assert plans == []


def test_track_row_with_no_pin_is_not_planned(db_session):
    from tests.conftest import make_skill

    skill = make_skill(db_session, slug="super-memory")
    bundle = make_bundle(db_session, name="some-bundle")
    make_bundle_skill(db_session, bundle, skill, pin_mode="track", pinned_version=None)

    plans = plan_track_pin_repair(db_session)
    assert plans == []


def test_track_pin_repair_scoped_to_bundle_slug(db_session):
    from tests.conftest import make_skill

    skill_a = make_skill(db_session, slug="ruthless-mentor")
    skill_b = make_skill(db_session, slug="super-memory")
    tori = make_bundle(db_session, name="tori-core", slug="tori-core")
    other = make_bundle(db_session, name="other-bundle", slug="other-bundle")
    make_bundle_skill(db_session, tori, skill_a, pin_mode="track", pinned_version="1.0.0")
    make_bundle_skill(db_session, other, skill_b, pin_mode="track", pinned_version="1.0.0")

    plans = plan_track_pin_repair(db_session, bundle_slug="tori-core")

    assert len(plans) == 1
    assert plans[0]["skill_slug"] == "ruthless-mentor"


def test_track_pin_repair_all_eight_torichain_entries(db_session):
    """Mirrors the real tori-core shape from SHARED_CONTEXT §1d: 8 declared
    skills, all pin_mode='track', most carrying a stale pinned_version."""
    from tests.conftest import make_skill

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
    for slug, pinned in declared:
        skill = make_skill(db_session, slug=slug)
        make_bundle_skill(db_session, tori, skill, pin_mode="track", pinned_version=pinned)

    plans = plan_track_pin_repair(db_session, bundle_slug="tori-core")

    # musk-5-step-algorithm has no stale pin — 7 of 8 need repair.
    assert len(plans) == 7
    assert {p["skill_slug"] for p in plans} == {
        "ruthless-mentor",
        "super-memory",
        "llm-wiki-hermes",
        "plan-for-goal",
        "hub-search-claude-code",
        "codex-sandbox-recovery",
        "loopskill",
    }

    apply_track_pin_repair(db_session, plans)
    db_session.flush()
    remaining = plan_track_pin_repair(db_session, bundle_slug="tori-core")
    assert remaining == []


# ── CLI wiring: dry-run is the default ───────────────────────────────────


def test_execute_and_fix_track_pins_default_to_false():
    from scripts.repair_dead_skill_version_paths import build_arg_parser

    args = build_arg_parser().parse_args([])
    assert args.execute is False
    assert args.fix_track_pins is False
    assert args.track_pins_only is False
    assert args.bundle_slug is None


def test_execute_flag_can_be_set():
    from scripts.repair_dead_skill_version_paths import build_arg_parser

    args = build_arg_parser().parse_args(["--execute", "--fix-track-pins", "--bundle-slug", "tori-core"])
    assert args.execute is True
    assert args.fix_track_pins is True
    assert args.bundle_slug == "tori-core"
