"""Tests for Issue #10 — Tarball scanner path-traversal detection.

TDD structure:
  _make_tarball()   — helper to build in-memory tarballs.
  test_pov_*        — proof-of-vulnerability (pre-fix).
  test_*            — regression tests that pass after fix.
"""
from __future__ import annotations

import io
import os
import tarfile
from typing import Optional

import pytest

from app.security_scan import Finding, scan_tarball


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tarball(members: list[dict]) -> bytes:
    """Build an in-memory .tar.gz from a list of member specs.

    Each spec dict may have:
      name      (str)  — member name / path
      content   (bytes) — file content (default b"harmless content")
      linkname  (str)  — symlink target (makes it a symlink if set)
      is_dir    (bool) — makes a directory member
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for spec in members:
            name = spec["name"]
            if spec.get("is_dir"):
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            elif spec.get("linkname") is not None:
                info = tarfile.TarInfo(name=name)
                info.type = tarfile.SYMTYPE
                info.linkname = spec["linkname"]
                tf.addfile(info)
            else:
                content = spec.get("content", b"harmless content\n")
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _has_critical_path_traversal(findings: list[Finding]) -> bool:
    return any(
        f.pattern_class == "path_traversal" and f.severity == "critical"
        for f in findings
    )


# ---------------------------------------------------------------------------
# PROOF OF VULNERABILITY (#10) — Pre-fix the scanner accepts these members.
# ---------------------------------------------------------------------------

ATTACK_MEMBERS = [
    {"name": "../../etc/passwd"},
    {"name": "/etc/passwd"},
    {"name": "foo/../../../etc/shadow"},
    {"name": "C:/Windows/system32/foo.sh"},
    {"name": "a/b/../../etc/cron.d/evil"},
]


@pytest.mark.parametrize("spec", ATTACK_MEMBERS, ids=[m["name"] for m in ATTACK_MEMBERS])
def test_pov_traversal_member_not_detected(spec):
    """POV #10: Before fix, scan_tarball does NOT return a CRITICAL finding.

    After the fix this test is automatically skipped.
    """
    tb = _make_tarball([spec])
    findings = scan_tarball(tb, {})
    has_critical = _has_critical_path_traversal(findings)
    if has_critical:
        pytest.skip("Fix already applied — path traversal is now detected")
    # Pre-fix: no critical finding (the vulnerability)
    assert not has_critical, "Pre-fix: expected no CRITICAL path_traversal finding"


def test_pov_symlink_traversal_not_detected():
    """POV #10: Symlink pointing outside sandbox not detected pre-fix."""
    tb = _make_tarball([{"name": "evil_link", "linkname": "../../../etc/passwd"}])
    findings = scan_tarball(tb, {})
    has_critical = _has_critical_path_traversal(findings)
    if has_critical:
        pytest.skip("Fix already applied")
    assert not has_critical


# ---------------------------------------------------------------------------
# REGRESSION TESTS — Pass only after the fix.
# ---------------------------------------------------------------------------

def test_absolute_path_member_raises_critical():
    """Issue #10 fix: member.name starting with '/' → CRITICAL finding."""
    tb = _make_tarball([{"name": "/etc/passwd", "content": b"root:x:0:0:"}])
    findings = scan_tarball(tb, {})
    assert _has_critical_path_traversal(findings), (
        f"Expected CRITICAL path_traversal finding, got: {findings}"
    )


def test_dotdot_parent_traversal_raises_critical():
    """Issue #10 fix: '../../etc/passwd' in member.name → CRITICAL finding."""
    tb = _make_tarball([{"name": "../../etc/passwd"}])
    findings = scan_tarball(tb, {})
    assert _has_critical_path_traversal(findings)


def test_dotdot_in_deep_path_raises_critical():
    """Issue #10 fix: 'a/b/../../etc/shadow' → CRITICAL finding."""
    tb = _make_tarball([{"name": "a/b/../../etc/shadow"}])
    findings = scan_tarball(tb, {})
    assert _has_critical_path_traversal(findings)


def test_drive_letter_path_raises_critical():
    """Issue #10 fix: 'C:/Windows/...' → CRITICAL finding (colon in name)."""
    tb = _make_tarball([{"name": "C:/Windows/system32/foo.sh"}])
    findings = scan_tarball(tb, {})
    assert _has_critical_path_traversal(findings)


def test_normpath_inconsistency_raises_critical():
    """Issue #10 fix: name that normalises differently → CRITICAL finding."""
    # './a/../b' normalises to 'a/b' — different from original
    tb = _make_tarball([{"name": "./a/../b"}])
    findings = scan_tarball(tb, {})
    assert _has_critical_path_traversal(findings)


def test_symlink_to_absolute_raises_critical():
    """Issue #10 fix: symlink with absolute target → CRITICAL finding."""
    tb = _make_tarball([{"name": "evil_link", "linkname": "/etc/passwd"}])
    findings = scan_tarball(tb, {})
    assert _has_critical_path_traversal(findings)


def test_symlink_dotdot_target_raises_critical():
    """Issue #10 fix: symlink with '../..' target → CRITICAL finding."""
    tb = _make_tarball([{"name": "evil_link", "linkname": "../../../etc/shadow"}])
    findings = scan_tarball(tb, {})
    assert _has_critical_path_traversal(findings)


def test_clean_tarball_passes():
    """Baseline: a tarball with only normal relative paths passes clean."""
    tb = _make_tarball([
        {"name": "setup.sh", "content": b"#!/bin/bash\necho hello\n"},
        {"name": "SKILL.md", "content": b"# My Skill\n"},
        {"name": "scripts/run.sh", "content": b"#!/bin/bash\npython3 main.py\n"},
        {"name": "references/README.md", "content": b"# Docs\n"},
    ])
    findings = scan_tarball(tb, {})
    traversal = [f for f in findings if f.pattern_class == "path_traversal"]
    assert not traversal, f"Unexpected path_traversal findings on clean tarball: {traversal}"


def test_multiple_attack_members_each_gets_finding():
    """Issue #10 fix: each attack variant in one tarball → multiple CRITICAL findings."""
    tb = _make_tarball([
        {"name": "../../etc/passwd"},
        {"name": "/etc/shadow"},
        {"name": "C:/Windows/foo"},
        {"name": "scripts/ok.sh", "content": b"echo ok"},  # clean
    ])
    findings = scan_tarball(tb, {})
    critical = [f for f in findings if f.pattern_class == "path_traversal" and f.severity == "critical"]
    assert len(critical) >= 3, f"Expected ≥3 critical findings, got: {critical}"
