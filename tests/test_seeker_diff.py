"""Tests for app.seeker.scan_vendor + diff_against_catalog."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.seeker import (
    InstalledSkill,
    diff_against_catalog,
    scan_vendor,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_skill(root: Path, slug: str, version: str | None = None,
                 description: str = "demo") -> Path:
    skill_dir = root / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = [f"name: {slug}", f"description: {description}"]
    if version is not None:
        fm_lines.append(f"version: {version}")
    body = "---\n" + "\n".join(fm_lines) + "\n---\n\n# body\n"
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(body, encoding="utf-8")
    return skill_md


@dataclass
class _FakeVersion:
    semver: str


@dataclass
class _FakeSkill:
    slug: str
    versions: list[_FakeVersion]
    rating_avg: float | None = None


# ── scan_vendor ─────────────────────────────────────────────────────────────


def test_scan_vendor_finds_skills_and_parses_frontmatter(tmp_path):
    _write_skill(tmp_path, "alpha", version="1.2.3", description="alpha skill")
    _write_skill(tmp_path, "beta", version="0.1.0", description="beta skill")

    found = scan_vendor(tmp_path, vendor="claude")
    by_name = {s.name: s for s in found}
    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"].version == "1.2.3"
    assert by_name["alpha"].vendor == "claude"
    assert by_name["alpha"].description == "alpha skill"


def test_scan_vendor_missing_path_returns_empty(tmp_path):
    assert scan_vendor(tmp_path / "nope", vendor="claude") == []


def test_scan_vendor_skips_malformed_frontmatter(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
    _write_skill(tmp_path, "good", version="1.0.0")

    found = scan_vendor(tmp_path, vendor="codex")
    assert [s.name for s in found] == ["good"]


def test_scan_vendor_skips_missing_name_field(tmp_path):
    no_name = tmp_path / "nameless"
    no_name.mkdir()
    body = "---\ndescription: missing name\n---\n\nbody\n"
    (no_name / "SKILL.md").write_text(body, encoding="utf-8")

    found = scan_vendor(tmp_path, vendor="hermes")
    assert found == []


# ── diff_against_catalog ────────────────────────────────────────────────────


def test_diff_flags_newer_catalog_version():
    installed = [InstalledSkill(vendor="claude", name="alpha", version="1.0.0", path="/x")]
    catalog = [_FakeSkill(slug="alpha", versions=[_FakeVersion("2.0.0")])]

    recs = diff_against_catalog(installed, catalog)
    assert len(recs) == 1
    assert recs[0].reason == "newer"
    assert recs[0].installed_version == "1.0.0"
    assert recs[0].catalog_version == "2.0.0"
    assert recs[0].vendor == "claude"


def test_diff_silent_when_versions_equal_and_no_quality_signal():
    installed = [InstalledSkill(vendor="codex", name="alpha", version="1.0.0", path="/x")]
    catalog = [_FakeSkill(slug="alpha", versions=[_FakeVersion("1.0.0")])]

    assert diff_against_catalog(installed, catalog) == []


def test_diff_emits_better_quality_when_rating_is_high_and_versions_equal():
    installed = [InstalledSkill(vendor="codex", name="alpha", version="1.0.0", path="/x")]
    catalog = [_FakeSkill(slug="alpha", versions=[_FakeVersion("1.0.0")], rating_avg=4.8)]

    recs = diff_against_catalog(installed, catalog)
    assert len(recs) == 1
    assert recs[0].reason == "better-quality"


def test_diff_marks_missing_when_catalog_lacks_skill():
    installed = [InstalledSkill(vendor="hermes", name="ghost", version="0.1.0", path="/x")]
    catalog = [_FakeSkill(slug="alpha", versions=[_FakeVersion("1.0.0")])]

    recs = diff_against_catalog(installed, catalog)
    assert len(recs) == 1
    assert recs[0].reason == "missing"
    assert recs[0].slug == "ghost"
    assert recs[0].catalog_version is None


def test_diff_handles_catalog_without_versions():
    installed = [InstalledSkill(vendor="claude", name="alpha", version="1.0.0", path="/x")]
    catalog = [_FakeSkill(slug="alpha", versions=[])]

    # No catalog version means the catalog is no better — emit nothing.
    assert diff_against_catalog(installed, catalog) == []


def test_diff_handles_non_semver_strings_via_lex_compare():
    installed = [InstalledSkill(vendor="opencode", name="alpha", version="alpha", path="/x")]
    catalog = [_FakeSkill(slug="alpha", versions=[_FakeVersion("beta")])]

    recs = diff_against_catalog(installed, catalog)
    assert len(recs) == 1
    assert recs[0].reason == "newer"
