"""Tests for scripts/skill_discipline_linter.py — Phase A.7 discipline gate.

Covers:
  - happy path (clean SKILL.md + recipe.yaml passes)
  - user-name violation rejected
  - curl|bash violation rejected
  - hardcoded /home/adam path → auto-fix replaces with ${HOME}
  - bonus: external-promo, internal-infra, agent-discipline, report-back rules
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure scripts/ is importable.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.skill_discipline_linter import (  # noqa: E402
    auto_fix,
    lint_skill,
    main,
)
import scripts.skill_discipline_linter as _linter  # noqa: E402


@pytest.fixture(autouse=True)
def _configure_linter_tokens(monkeypatch):
    """Populate the env-driven banned-token lists with the tokens these tests probe.

    The linter ships with EMPTY defaults (no real operator names in the OSS tree);
    tests configure their own fixture tokens so the detection mechanism is verified.
    """
    monkeypatch.setattr(_linter, "USER_NAMES", ("Adam", "Tori", "Wise", "Chef"))
    monkeypatch.setattr(_linter, "INTERNAL_INFRA", ("Paperclip", "wisechef-hq", "adam-xps"))
    yield


CLEAN_RECIPE_YAML = """\
name: example-skill
version: 1.0.0
runtime:
  compatibility:
    os: [linux, darwin]
    arch: [x86_64, arm64]
    ram_gb: 1
    network: required
"""

CLEAN_SKILL_MD = """\
# Example Skill

A portable skill that does one thing well.

## Install

Download from https://github.com/example/example-skill/releases.
Verify the checksum, then run the installer.

## Usage

Run `example-skill --help` to see all options. Output goes to
${OPERATOR_NOTIFY_CHANNEL}.

## Pitfalls

- Requires Python 3.10+.
"""


# ── Happy path ─────────────────────────────────────────────────────────────


def test_clean_skill_passes() -> None:
    result = lint_skill(CLEAN_SKILL_MD, recipe_yaml=CLEAN_RECIPE_YAML)
    assert result["ok"], f"Expected clean skill to pass; violations: {result['violations']}"
    assert result["violations"] == []


# ── Rule: no_user_names ────────────────────────────────────────────────────


def test_user_name_violation_rejected() -> None:
    bad = CLEAN_SKILL_MD + "\nWhen done, ping Adam in the standup.\n"
    result = lint_skill(bad, recipe_yaml=CLEAN_RECIPE_YAML)
    assert not result["ok"]
    rules = {v["rule"] for v in result["violations"]}
    assert "no_user_names" in rules


def test_user_name_in_email_is_not_flagged() -> None:
    """Bare-name match must skip occurrences inside email addresses."""
    bad = CLEAN_SKILL_MD + "\nContact: support@adam-corp.example.\n"
    # support@adam-corp.example is an email — Adam should not be flagged
    # because the regex masks URL/email spans before testing names.
    result = lint_skill(bad, recipe_yaml=CLEAN_RECIPE_YAML)
    no_user_violations = [v for v in result["violations"] if v["rule"] == "no_user_names"]
    assert no_user_violations == []


# ── Rule: no_curl_bash ─────────────────────────────────────────────────────


def test_curl_bash_violation_rejected() -> None:
    bad = CLEAN_SKILL_MD + "\nInstall: `curl -L https://example.com/inst.sh | bash`\n"
    result = lint_skill(bad, recipe_yaml=CLEAN_RECIPE_YAML)
    assert not result["ok"]
    rules = {v["rule"] for v in result["violations"]}
    assert "no_curl_bash" in rules


def test_wget_pipe_sh_also_rejected() -> None:
    bad = CLEAN_SKILL_MD + "\nOr: `wget -O- https://example.com/inst | sh`\n"
    result = lint_skill(bad, recipe_yaml=CLEAN_RECIPE_YAML)
    assert not result["ok"]
    rules = {v["rule"] for v in result["violations"]}
    assert "no_curl_bash" in rules


# ── Rule: no_hardcoded_home_paths + auto-fix ───────────────────────────────


def test_hardcoded_home_path_violation_and_autofix() -> None:
    bad = CLEAN_SKILL_MD + "\nConfig lives at /home/adam/.config/example.toml\n"
    result = lint_skill(bad, recipe_yaml=CLEAN_RECIPE_YAML)
    assert not result["ok"]
    rules = {v["rule"] for v in result["violations"]}
    assert "no_hardcoded_home_paths" in rules

    fixed = auto_fix(bad)
    assert "/home/adam/" not in fixed
    assert "${HOME}/.config/example.toml" in fixed

    # After auto-fix, this rule is no longer triggered.
    refixed_result = lint_skill(fixed, recipe_yaml=CLEAN_RECIPE_YAML)
    refixed_rules = {v["rule"] for v in refixed_result["violations"]}
    assert "no_hardcoded_home_paths" not in refixed_rules


# ── Rule: must_declare_compat ──────────────────────────────────────────────


def test_missing_recipe_compatibility_is_blocking() -> None:
    incomplete = """\
name: incomplete
version: 1.0.0
runtime:
  compatibility:
    os: [linux]
"""
    result = lint_skill(CLEAN_SKILL_MD, recipe_yaml=incomplete)
    assert not result["ok"]
    rules = {v["rule"] for v in result["violations"]}
    assert "must_declare_compat" in rules


def test_missing_recipe_yaml_entirely_is_blocking() -> None:
    result = lint_skill(CLEAN_SKILL_MD, recipe_yaml=None)
    rules = {v["rule"] for v in result["violations"]}
    assert "must_declare_compat" in rules


# ── Rule: no_internal_infra_refs ───────────────────────────────────────────


def test_internal_infra_reference_rejected() -> None:
    bad = CLEAN_SKILL_MD + "\nFile the issue in Paperclip when blocked.\n"
    result = lint_skill(bad, recipe_yaml=CLEAN_RECIPE_YAML)
    assert not result["ok"]
    rules = {v["rule"] for v in result["violations"]}
    assert "no_internal_infra_refs" in rules


# ── Rule: no_agent_discipline_text ─────────────────────────────────────────


def test_agent_discipline_text_rejected() -> None:
    bad = CLEAN_SKILL_MD + "\nThe agent should always check before continuing.\n"
    result = lint_skill(bad, recipe_yaml=CLEAN_RECIPE_YAML)
    rules = {v["rule"] for v in result["violations"]}
    assert "no_agent_discipline_text" in rules


# ── Rule: no_external_promo ────────────────────────────────────────────────


def test_external_promo_link_rejected() -> None:
    bad = CLEAN_SKILL_MD + "\nMore at https://my-affiliate.example.com/?ref=adam.\n"
    result = lint_skill(bad, recipe_yaml=CLEAN_RECIPE_YAML)
    rules = {v["rule"] for v in result["violations"]}
    assert "no_external_promo" in rules


def test_allowlisted_domains_pass() -> None:
    ok = CLEAN_SKILL_MD + "\nSee https://github.com/example/example, https://pypi.org/project/x.\n"
    result = lint_skill(ok, recipe_yaml=CLEAN_RECIPE_YAML)
    rules = {v["rule"] for v in result["violations"]}
    assert "no_external_promo" not in rules


# ── Rule: no_report_back_without_placeholder ───────────────────────────────


def test_report_back_to_named_person_rejected() -> None:
    bad = CLEAN_SKILL_MD + "\nWhen finished, report to Mariusz.\n"
    result = lint_skill(bad, recipe_yaml=CLEAN_RECIPE_YAML)
    rules = {v["rule"] for v in result["violations"]}
    assert "no_report_back_without_placeholder" in rules


def test_report_back_to_placeholder_passes() -> None:
    ok = CLEAN_SKILL_MD + "\nWhen finished, report to ${OPERATOR_NOTIFY_CHANNEL}.\n"
    result = lint_skill(ok, recipe_yaml=CLEAN_RECIPE_YAML)
    rules = {v["rule"] for v in result["violations"]}
    assert "no_report_back_without_placeholder" not in rules


# ── CLI entry point ────────────────────────────────────────────────────────


def test_cli_passes_clean_skill(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(CLEAN_SKILL_MD)
    (skill / "recipe.yaml").write_text(CLEAN_RECIPE_YAML)
    rc = main([str(skill)])
    assert rc == 0


def test_cli_rejects_dirty_skill(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(CLEAN_SKILL_MD + "\nAsk Adam for the answer.\n")
    (skill / "recipe.yaml").write_text(CLEAN_RECIPE_YAML)
    rc = main([str(skill)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no_user_names" in captured.out


def test_cli_auto_fix_emits_diff(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text(CLEAN_SKILL_MD + "\nAt /home/adam/.config/x\n")
    rc = main([str(f), "--auto-fix"])
    captured = capsys.readouterr()
    assert rc == 1  # diff non-empty → exit 1 to flag changes pending
    assert "/home/adam/" in captured.out  # original line in diff
    assert "${HOME}/" in captured.out
