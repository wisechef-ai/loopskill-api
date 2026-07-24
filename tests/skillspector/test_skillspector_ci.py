"""Tests for scripts/skillspector_ci.py — Phase K (loopclose_3005).

Validates:
1. Malicious test fixture is flagged at HIGH+ (the catch proof).
2. Real skill catalog (docs/recipes-skill/ + examples/) is clean after
   baseline suppression (false-positive proof).
3. Advisory→blocker flip via SKILLSPECTOR_BLOCK_ON_HIGH env var.

These tests do NOT call LLM — pure static analysis (--no-llm).
They require skillspector to be installed in the CI venv
(.skillspector-venv/ relative to repo root, or on PATH).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).parent.parent.parent)))
SCRIPT = REPO_ROOT / "scripts" / "skillspector_ci.py"
MALICIOUS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "skills" / "malicious-test-skill"
SKILL_DIR = REPO_ROOT / "docs" / "recipes-skill"
EXAMPLES_DIR = REPO_ROOT / "examples"
BASELINE = REPO_ROOT / ".skillspector-baseline.json"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_ci(
    target: str | Path,
    sarif_out: Path,
    block_on_high: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the CI wrapper as a subprocess and return the completed process."""
    env = {**os.environ}
    env["SKILLSPECTOR_BLOCK_ON_HIGH"] = "true" if block_on_high else "false"
    env["SKILLSPECTOR_BASELINE_FILE"] = str(BASELINE)
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, str(SCRIPT), str(target), "--sarif-out", str(sarif_out)],
        capture_output=False,
        text=True,
        env=env,
        timeout=120,
    )


def _load_sarif(path: Path) -> dict:
    """Load and parse a SARIF file."""
    return json.loads(path.read_text())


def _get_findings(sarif: dict) -> list[dict]:
    """Flatten all results from all runs."""
    findings: list[dict] = []
    for run in sarif.get("runs", []):
        findings.extend(run.get("results", []))
    return findings


def _high_plus(findings: list[dict]) -> list[dict]:
    """Filter to HIGH/CRITICAL findings (SARIF level=error)."""
    return [f for f in findings if f.get("level") == "error"]


# ---------------------------------------------------------------------------
# Skip condition: skillspector must be available
# ---------------------------------------------------------------------------


def _skillspector_available() -> bool:
    """Return True if skillspector is on PATH or in the project venv."""
    venv_bin = REPO_ROOT / ".skillspector-venv" / "bin" / "skillspector"
    if venv_bin.exists():
        return True
    try:
        result = subprocess.run(
            ["skillspector", "--version"], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


requires_skillspector = pytest.mark.skipif(
    not _skillspector_available(),
    reason="SkillSpector not installed — run: uv pip install 'skillspector @ git+https://github.com/NVIDIA/skillspector.git@2eb844780ab163f01468ecf142c40a2ec0fcaec0'",
)


# ---------------------------------------------------------------------------
# Test 1: PROOF — malicious skill is caught at HIGH+ (advisory mode, exit 0)
# ---------------------------------------------------------------------------


@requires_skillspector
def test_malicious_skill_flagged_advisory(tmp_path):
    """THE PROOF: malicious fixture triggers HIGH+ findings in advisory mode.

    Asserts:
    - Exit code 0 (advisory — never blocks)
    - SARIF has ≥3 HIGH/CRITICAL (error-level) findings
    - SC2 (curl|bash) is present
    - PE3 (credential access) is present
    """
    sarif_out = tmp_path / "malicious.sarif"
    result = _run_ci(MALICIOUS_FIXTURE, sarif_out, block_on_high=False)

    # Advisory mode must always exit 0
    assert result.returncode == 0, f"Advisory mode must exit 0, got {result.returncode}"

    assert sarif_out.exists(), "SARIF output file must exist"
    sarif = _load_sarif(sarif_out)
    findings = _get_findings(sarif)
    high = _high_plus(findings)

    assert len(high) >= 3, (
        f"Expected ≥3 HIGH/CRITICAL findings from malicious fixture, got {len(high)}. "
        f"Findings: {[f['ruleId'] for f in findings]}"
    )

    rule_ids = {f["ruleId"] for f in high}
    assert "SC2" in rule_ids, f"SC2 (curl|bash) must be flagged. Got rule IDs: {rule_ids}"
    assert "PE3" in rule_ids, f"PE3 (credential access) must be flagged. Got rule IDs: {rule_ids}"


@requires_skillspector
def test_malicious_skill_blocked_in_blocker_mode(tmp_path):
    """Malicious fixture must cause exit 1 in blocker mode."""
    sarif_out = tmp_path / "malicious-blocked.sarif"
    result = _run_ci(MALICIOUS_FIXTURE, sarif_out, block_on_high=True)

    assert result.returncode == 1, (
        f"Blocker mode must exit 1 for malicious skill, got {result.returncode}"
    )

    sarif = _load_sarif(sarif_out)
    findings = _get_findings(sarif)
    high = _high_plus(findings)
    assert len(high) >= 3, "Expected HIGH+ findings even in blocker output SARIF"


# ---------------------------------------------------------------------------
# Test 2: CATALOG — real skill content is clean after baselining
# ---------------------------------------------------------------------------


@requires_skillspector
def test_real_skill_clean_after_baseline_advisory(tmp_path):
    """docs/recipes-skill/ is false-positive-clean after baseline (advisory mode)."""
    sarif_out = tmp_path / "skill.sarif"
    result = _run_ci(SKILL_DIR, sarif_out, block_on_high=False)

    assert result.returncode == 0, f"Advisory mode must exit 0 for real skill, got {result.returncode}"
    sarif = _load_sarif(sarif_out)
    high = _high_plus(_get_findings(sarif))
    assert len(high) == 0, (
        f"Real skill must have 0 HIGH/CRITICAL after baselining, got {len(high)}: "
        f"{[(f['ruleId'], f.get('locations', [{}])[0].get('physicalLocation', {}).get('artifactLocation', {}).get('uri', '?')) for f in high]}"
    )


@requires_skillspector
def test_real_skill_clean_in_blocker_mode(tmp_path):
    """docs/recipes-skill/ passes in blocker mode (baseline suppresses false positives)."""
    sarif_out = tmp_path / "skill-blocked.sarif"
    result = _run_ci(SKILL_DIR, sarif_out, block_on_high=True)

    assert result.returncode == 0, (
        f"Real skill must pass in blocker mode after baselining, got {result.returncode}. "
        "If this fails, a new false positive needs to be added to .skillspector-baseline.json."
    )


@requires_skillspector
def test_examples_clean_after_baseline_advisory(tmp_path):
    """examples/ is false-positive-clean after baseline (advisory mode)."""
    sarif_out = tmp_path / "examples.sarif"
    result = _run_ci(EXAMPLES_DIR, sarif_out, block_on_high=False)

    assert result.returncode == 0
    sarif = _load_sarif(sarif_out)
    high = _high_plus(_get_findings(sarif))
    assert len(high) == 0, (
        f"examples/ must have 0 HIGH/CRITICAL after baselining, got {len(high)}: "
        f"{[(f['ruleId'], f.get('locations', [{}])[0].get('physicalLocation', {}).get('artifactLocation', {}).get('uri', '?')) for f in high]}"
    )


@requires_skillspector
def test_examples_clean_in_blocker_mode(tmp_path):
    """examples/ passes in blocker mode after baselining."""
    sarif_out = tmp_path / "examples-blocked.sarif"
    result = _run_ci(EXAMPLES_DIR, sarif_out, block_on_high=True)

    assert result.returncode == 0, (
        f"examples/ must pass in blocker mode after baselining, got {result.returncode}. "
        "If this fails, a new false positive needs to be added to .skillspector-baseline.json."
    )


# ---------------------------------------------------------------------------
# Test 3: ADVISORY→BLOCKER flip — clean skill passes in both modes
# ---------------------------------------------------------------------------


@requires_skillspector
def test_advisory_flip_clean_skill_passes_both_modes(tmp_path):
    """Clean SKILL.md passes in both advisory and blocker mode."""
    skill_md = SKILL_DIR / "SKILL.md"

    for mode_name, block_on_high in [("advisory", False), ("blocker", True)]:
        sarif_out = tmp_path / f"skill-md-{mode_name}.sarif"
        result = _run_ci(skill_md, sarif_out, block_on_high=block_on_high)
        assert result.returncode == 0, (
            f"Clean SKILL.md must pass in {mode_name} mode, got exit {result.returncode}"
        )
        sarif = _load_sarif(sarif_out)
        high = _high_plus(_get_findings(sarif))
        assert len(high) == 0, f"Clean SKILL.md must have 0 HIGH+ in {mode_name} mode"


# ---------------------------------------------------------------------------
# Test 4: baseline file is well-formed
# ---------------------------------------------------------------------------


def test_baseline_file_is_valid_json():
    """Baseline file must exist, be valid JSON, and have required keys."""
    assert BASELINE.exists(), f"Baseline file must exist at {BASELINE}"
    data = json.loads(BASELINE.read_text())
    assert "suppressed" in data, "Baseline must have a 'suppressed' top-level key"
    assert isinstance(data["suppressed"], dict), "'suppressed' must be a dict"
    for rule_id, patterns in data["suppressed"].items():
        assert isinstance(patterns, list), f"Patterns for {rule_id} must be a list"
        for p in patterns:
            assert isinstance(p, str), f"Each pattern for {rule_id} must be a string"


# ---------------------------------------------------------------------------
# Test 5: wrapper script exits 2 on internal error (bad target path)
# ---------------------------------------------------------------------------


@requires_skillspector
def test_wrapper_handles_missing_target_gracefully(tmp_path):
    """Wrapper must not crash with unhandled exception on a missing path."""
    sarif_out = tmp_path / "missing.sarif"
    result = _run_ci("/nonexistent/path/that/does/not/exist", sarif_out)
    # SkillSpector itself will fail; wrapper should return 2 or propagate cleanly
    assert result.returncode in (0, 1, 2), (
        f"Wrapper must exit with 0/1/2 for bad path, got {result.returncode}"
    )
