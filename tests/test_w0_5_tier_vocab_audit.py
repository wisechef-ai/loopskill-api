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
            'TIER_MAP = {\n'
            '    "cook": "pro",\n'
            '    "operator": "pro_plus",\n'
            '}  # legacy alias map\n',
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
            "TIERS = [  # legacy alias map (sunset 2026-06-10)\n"
            '    "cook",\n'
            '    "operator",\n'
            "]\n",
            encoding="utf-8",
        )
        assert audit.scan(tmp_path) == []


class TestRealViolationsStillCaught:
    def test_marker_free_slug_is_flagged(self, audit, tmp_path):
        """The gate must NOT become a no-op — a bare legacy slug still trips."""
        f = tmp_path / "bare.py"
        f.write_text(
            'DEFAULT_TIER = "operator"\n'
            'OTHER = "cook"\n',
            encoding="utf-8",
        )
        violations = audit.scan(tmp_path)
        flagged_lines = {ln for _f, ln, _line in violations}
        assert flagged_lines == {1, 2}, (
            f"bare legacy slugs were not all flagged: {violations}"
        )

    def test_marker_far_away_does_not_suppress(self, audit, tmp_path):
        """A marker >2 lines from the slug must NOT suppress it."""
        f = tmp_path / "far.py"
        f.write_text(
            "# legacy alias map\n"
            "x = 1\n"
            "y = 2\n"
            "z = 3\n"
            'DEFAULT = "operator"\n',
            encoding="utf-8",
        )
        violations = audit.scan(tmp_path)
        assert any(ln == 5 for _f, ln, _line in violations), (
            "a marker 4 lines away wrongly suppressed the violation"
        )


class TestLiveTreeClean:
    def test_repo_passes_audit(self, audit):
        violations = audit.scan(REPO_ROOT)
        assert violations == [], (
            "live tree has legacy tier vocab outside the SSOT/alias-map: "
            + "; ".join(f"{f}:{ln}" for f, ln, _ in violations[:20])
        )
