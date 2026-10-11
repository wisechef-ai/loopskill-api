"""W0.5 (integrator_2905) — tier-vocab audit format-stability regression.

scripts/audit_tier_vocab.py enforces that legacy tier slugs (cook|operator|
studio) appear only inside config/tiers.yaml or alongside an alias-map marker.
The original matcher checked the marker on the SAME line as the slug — so a
ruff-format pass that split a dict/enum literal across lines (moving the
trailing ``# legacy alias`` comment away from the slug) re-tripped the gate on
code that was already correct. W0.5 widened the check to a +/-2-line window.

This test pins both halves of the contract so a future audit refactor can't
regress either way:

  1. A legacy slug WITH an alias marker within 2 lines is NOT a violation
     (format-stable — the real-world false positive W0.5 fixed).
  2. A legacy slug with NO marker anywhere near it IS still a violation
     (the gate must not become a no-op).

It also runs the audit against the live tree and asserts it is clean, so the
SSOT stays honest.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT_PATH = REPO_ROOT / "scripts" / "audit_tier_vocab.py"


def _load_audit():
    spec = importlib.util.spec_from_file_location("audit_tier_vocab", AUDIT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_tier_vocab"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit():
    return _load_audit()


class TestWindowedMarkerSuppression:
    def test_marker_two_lines_below_suppresses(self, audit, tmp_path):
        """A slug whose alias marker landed 2 lines below (ruff wrap) is clean."""
        f = tmp_path / "wrapped.py"
        f.write_text(
            'TIER_MAP = {\n    "cook": "pro",\n    "operator": "pro_plus",\n}  # legacy alias map\n',
            encoding="utf-8",
        )
        violations = audit.scan(tmp_path)
        assert violations == [], (
            f"windowed marker suppression failed — wrapped alias map flagged: {violations}"
        )

    def test_marker_two_lines_above_suppresses(self, audit, tmp_path):
        f = tmp_path / "above.py"
        # Marker on the line directly opening the list; both slugs fall within
        # the +/-2 window of it (cook at +1, operator at +2).
        f.write_text(
            'TIERS = [  # legacy alias map (sunset 2026-06-10)\n    "cook",\n    "operator",\n]\n',
            encoding="utf-8",
        )
        assert audit.scan(tmp_path) == []


class TestRealViolationsStillCaught:
    def test_marker_free_slug_is_flagged(self, audit, tmp_path):
        """The gate must NOT become a no-op — a bare legacy slug still trips."""
        f = tmp_path / "bare.py"
        f.write_text(
            'DEFAULT_TIER = "operator"\nOTHER = "cook"\n',
            encoding="utf-8",
        )
        violations = audit.scan(tmp_path)
        flagged_lines = {ln for _f, ln, _line in violations}
        assert flagged_lines == {1, 2}, f"bare legacy slugs were not all flagged: {violations}"

    def test_marker_far_away_does_not_suppress(self, audit, tmp_path):
        """A marker >2 lines from the slug must NOT suppress it."""
        f = tmp_path / "far.py"
        f.write_text(
            '# legacy alias map\nx = 1\ny = 2\nz = 3\nDEFAULT = "operator"\n',
            encoding="utf-8",
        )
        violations = audit.scan(tmp_path)
        assert any(ln == 5 for _f, ln, _line in violations), (
            "a marker 4 lines away wrongly suppressed the violation"
        )


class TestGitignoredPathsSkipped:
    """The live-tree walk must not audit files git ignores (2026-10-11).

    A local venv created inside the tree (packaging/loopskill-mcp/.venv) put
    thousands of site-packages files under the root; their incidental use of
    the words 'operator'/'studio' (import operator, the ``>`` operator, ...)
    failed test_repo_passes_audit on that one machine while CI (fresh
    checkout, no venv) stayed green. Git is the source of truth for "not part
    of this repo": untracked-and-ignored paths are skipped, tracked ones are
    not, and the gate degrades to the old prefix skip when git is missing.
    """

    def _git_repo_with_venv(self, tmp_path: Path) -> Path:
        import subprocess

        root = tmp_path / "repo"
        root.mkdir()
        (root / ".gitignore").write_text("ignored_dir/\n", encoding="utf-8")
        proc = subprocess.run(["git", "init", "-q", str(root)], capture_output=True)
        assert proc.returncode == 0, f"git init failed: {proc.stderr!r}"
        # .gitignore must be TRACKED for ls-files to apply it.
        subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True, capture_output=True)
        (root / "ignored_dir").mkdir()
        (root / "ignored_dir" / "vendor.py").write_text(
            'import operator\nTIER = "operator"\n', encoding="utf-8"
        )
        return root

    def test_gitignored_violation_is_skipped_tracked_one_still_flagged(self, audit, tmp_path):
        root = self._git_repo_with_venv(tmp_path)
        (root / "app_code.py").write_text('TIER = "operator"\n', encoding="utf-8")

        flagged = {str(f) for f, _ln, _line in audit.scan(root)}

        assert str(root / "app_code.py") in flagged, (
            "gate became a no-op: a TRACKED legacy slug went unflagged"
        )
        assert str(root / "ignored_dir" / "vendor.py") not in flagged, (
            "gitignored local-env file was audited (the machine-dependent false positive)"
        )

    def test_falls_back_to_prefix_skip_when_git_unavailable(self, audit, tmp_path, monkeypatch):
        import subprocess as sp

        root = self._git_repo_with_venv(tmp_path)

        def _no_git(*_a, **_k):  # simulate a checkout without git
            raise OSError("git not installed")

        monkeypatch.setattr(sp, "run", _no_git)
        # .venv/ is covered by SKIP_DIR_PREFIXES, so the fallback still skips it.
        venv_probe = root / ".venv" / "lib.py"
        venv_probe.parent.mkdir()
        venv_probe.write_text("import operator\n", encoding="utf-8")

        flagged = {str(f) for f, _ln, _line in audit.scan(root)}

        assert str(venv_probe) not in flagged, "git-less fallback lost the .venv/ prefix skip"


class TestLiveTreeClean:
    def test_repo_passes_audit(self, audit):
        violations = audit.scan(REPO_ROOT)
        assert violations == [], "live tree has legacy tier vocab outside the SSOT/alias-map: " + "; ".join(
            f"{f}:{ln}" for f, ln, _ in violations[:20]
        )
