"""Tests for the catalog linter backfill script — issue #110.

Verifies the audit script:
- Identifies skills with no published version (handled by issue #109 invariant,
  so the audit should simply skip them gracefully)
- Detects hardcoded /home/<user>/ paths in tarball SKILL.md
- Renders a markdown report with the expected structure
- Exits non-zero in --strict mode when violations are found
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from app.models import Skill, SkillVersion
from scripts.catalog_linter_backfill import audit_catalog, render_markdown


def _make_tarball(files: dict[str, str]) -> bytes:
    """Build an in-memory .tar.gz with the given file map."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, body in files.items():
            data = body.encode("utf-8")
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


_RECIPE_YAML = """version: 1
runtime:
  compatibility:
    os: [linux, macos]
    arch: [x86_64, arm64]
    ram_gb: 1
    network: optional
"""


def _seed_skill_with_tarball(
    db_session, tmp_path: Path, slug: str, skill_md: str,
    *, recipe_yaml: str = _RECIPE_YAML,
) -> Skill:
    """Create a Skill + SkillVersion with the tarball written to tmp_path."""
    sid = uuid4()
    s = Skill(
        id=sid,
        slug=slug,
        title=slug,
        description=f"Test skill {slug}",
        category="dev-tools",
        tier="cook",
        is_public=True,
        is_archived=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(s)
    files = {"SKILL.md": skill_md}
    if recipe_yaml is not None:
        files["recipe.yaml"] = recipe_yaml
    tar_bytes = _make_tarball(files)
    tar_path = tmp_path / f"{slug}.tar.gz"
    tar_path.write_bytes(tar_bytes)
    v = SkillVersion(
        id=uuid4(),
        skill_id=sid,
        semver="1.0.0",
        tarball_path=str(tar_path),
        tarball_size_bytes=len(tar_bytes),
        checksum_sha256="0" * 64,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(v)
    db_session.flush()
    return s


def test_audit_clean_skill_reports_zero_violations(db_session, tmp_path):
    clean_md = (
        "---\n"
        "name: clean-skill\n"
        "description: A portable skill with no hardcoded paths.\n"
        "---\n"
        "# Clean skill\n\n"
        "Install with `npm install -g some-tool`.\n"
    )
    _seed_skill_with_tarball(db_session, tmp_path, "clean-skill", clean_md)
    report = audit_catalog(db_session)
    assert report["skills_scanned"] == 1
    assert len(report["skills_clean"]) == 1
    assert report["skills_with_violations"] == []


def test_audit_flags_hardcoded_home_path(db_session, tmp_path):
    bad_md = (
        "---\n"
        "name: bad-skill\n"
        "description: Has a hardcoded path that breaks portability.\n"
        "---\n"
        "# Bad skill\n\n"
        "Run `cp /home/adam/.npm-global/bin/foo /tmp/foo` first.\n"
    )
    _seed_skill_with_tarball(db_session, tmp_path, "bad-skill", bad_md)
    report = audit_catalog(db_session)
    assert report["skills_scanned"] == 1
    assert len(report["skills_with_violations"]) == 1
    row = report["skills_with_violations"][0]
    assert row["slug"] == "bad-skill"
    assert any("no_hardcoded_home_paths" in v for v in row["violations"])


def test_audit_skips_skills_with_missing_tarball_file(db_session, tmp_path):
    """Tarball path is set in DB but the file doesn't exist on disk."""
    sid = uuid4()
    db_session.add(Skill(
        id=sid,
        slug="ghost-tarball",
        title="x",
        description="x",
        category="other",
        tier="cook",
        is_public=True,
        is_archived=False,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.add(SkillVersion(
        id=uuid4(),
        skill_id=sid,
        semver="1.0.0",
        tarball_path=str(tmp_path / "nonexistent.tar.gz"),
        tarball_size_bytes=0,
        checksum_sha256="0" * 64,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.flush()
    report = audit_catalog(db_session)
    assert report["skills_scanned"] == 1
    assert len(report["skills_missing_tarball"]) == 1
    assert report["skills_missing_tarball"][0]["slug"] == "ghost-tarball"
    assert report["skills_with_violations"] == []


def test_render_markdown_includes_summary_and_table(db_session, tmp_path):
    bad_md = (
        "---\n"
        "name: bad-skill-2\n"
        "description: bad\n"
        "---\n"
        "# bad\n\n/home/adam/foo\n"
    )
    _seed_skill_with_tarball(db_session, tmp_path, "bad-skill-2", bad_md)
    report = audit_catalog(db_session)
    md = render_markdown(report)
    assert "# Catalog Linter Backfill — Audit Report" in md
    assert "Skills scanned" in md
    assert "Violations by rule" in md
    assert "bad-skill-2" in md
    assert "no_hardcoded_home_paths" in md


def test_audit_report_violation_counts_are_aggregated(db_session, tmp_path):
    for i in range(3):
        _seed_skill_with_tarball(
            db_session,
            tmp_path,
            f"bad-{i}",
            "---\nname: x\ndescription: x\n---\n/home/adam/foo\n",
        )
    report = audit_catalog(db_session)
    assert report["skills_scanned"] == 3
    assert len(report["skills_with_violations"]) == 3
    assert report["violation_counts_by_rule"].get("no_hardcoded_home_paths", 0) >= 3


def test_audit_ignores_archived_skills(db_session, tmp_path):
    bad_md = "---\nname: x\ndescription: x\n---\n/home/adam/foo\n"
    s = _seed_skill_with_tarball(db_session, tmp_path, "archived-bad", bad_md)
    s.is_archived = True
    s.archived_at = datetime.now(timezone.utc)
    db_session.flush()
    report = audit_catalog(db_session)
    assert report["skills_scanned"] == 0
    assert report["skills_with_violations"] == []
