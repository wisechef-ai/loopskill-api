"""Tests for the loop-manifest safety-bounded contract validator.

loopskill_0622 Phase 8. The validator is what makes the loop registry *vetted*:
no verification, no bound, no allow-list => rejected.
"""
from __future__ import annotations

import pytest

from app.loop_validation import (
    MAX_TURNS_CEILING,
    LoopValidationError,
    validate_loop_manifest,
)


def _valid_manifest(**overrides):
    base = {
        "success_condition": "all tests pass",
        "verification_script": "pytest -q",
        "system_prompt": "You are a TDD loop. Make the tests pass.",
        "max_turns": 25,
        "budget_usd": 5.0,
        "tool_allowlist": ["terminal", "read_file"],
        "stopping_criteria": {
            "success": "pytest exits 0",
            "failure": "same failure twice",
            "budget": "5 USD",
        },
    }
    base.update(overrides)
    return base


def test_valid_manifest_passes_and_normalizes():
    out = validate_loop_manifest(_valid_manifest())
    assert out["max_turns"] == 25
    assert out["budget_usd"] == 5.0
    assert out["tool_allowlist"] == ["terminal", "read_file"]
    assert set(out["stopping_criteria"]) == {"success", "failure", "budget"}


def test_missing_verification_script_is_rejected():
    with pytest.raises(LoopValidationError, match="verification_script"):
        validate_loop_manifest(_valid_manifest(verification_script=""))


def test_missing_success_condition_is_rejected():
    with pytest.raises(LoopValidationError, match="success_condition"):
        validate_loop_manifest(_valid_manifest(success_condition="   "))


def test_missing_system_prompt_is_rejected():
    with pytest.raises(LoopValidationError, match="system_prompt"):
        validate_loop_manifest(_valid_manifest(system_prompt=""))


def test_zero_max_turns_is_rejected():
    with pytest.raises(LoopValidationError, match="max_turns"):
        validate_loop_manifest(_valid_manifest(max_turns=0))


def test_negative_max_turns_is_rejected():
    with pytest.raises(LoopValidationError, match="max_turns"):
        validate_loop_manifest(_valid_manifest(max_turns=-5))


def test_max_turns_over_ceiling_is_rejected():
    with pytest.raises(LoopValidationError, match="ceiling"):
        validate_loop_manifest(_valid_manifest(max_turns=MAX_TURNS_CEILING + 1))


def test_tool_allowlist_omitted_is_rejected():
    m = _valid_manifest()
    del m["tool_allowlist"]
    with pytest.raises(LoopValidationError, match="tool_allowlist"):
        validate_loop_manifest(m)


def test_empty_tool_allowlist_is_allowed():
    out = validate_loop_manifest(_valid_manifest(tool_allowlist=[]))
    assert out["tool_allowlist"] == []


def test_stopping_criteria_missing_keys_rejected():
    with pytest.raises(LoopValidationError, match="missing required keys"):
        validate_loop_manifest(
            _valid_manifest(stopping_criteria={"success": "x"})
        )


def test_stopping_criteria_not_object_rejected():
    with pytest.raises(LoopValidationError, match="stopping_criteria"):
        validate_loop_manifest(_valid_manifest(stopping_criteria="done"))


def test_budget_optional_when_max_turns_present():
    out = validate_loop_manifest(_valid_manifest(budget_usd=None))
    assert out["budget_usd"] is None
    assert out["max_turns"] == 25  # max_turns is the backstop


def test_negative_budget_rejected():
    with pytest.raises(LoopValidationError, match="budget_usd"):
        validate_loop_manifest(_valid_manifest(budget_usd=-1))


def test_non_dict_manifest_rejected():
    with pytest.raises(LoopValidationError, match="object"):
        validate_loop_manifest("not a dict")  # type: ignore[arg-type]
