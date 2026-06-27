"""Regression guard for the seeded starter-loop library (loopskill_run_0627).

Each seeded loop ships a verification_script that the runner executes to produce
an objective pass/fail. A subtly-broken script (shell/Python quoting, a typo)
would make a loop either un-runnable or vacuously-passing. These tests assert,
for every seeded loop, that:

  1. its safety contract validates (validate_loop_manifest),
  2. its verification_script RUNS and PASSES on a known-good fixture, and
  3. (for loops with a meaningful negative case) it FAILS on a known-bad fixture
     — proving the verification is not vacuous.

Runs in bounded mode (CI has no kernel sandbox), exercising the real executor.
"""

from __future__ import annotations


import pytest

from app.loop_runner import LoopRunner
from app.loop_validation import LoopValidationError, validate_loop_manifest
from scripts.seed_starter_catalog import STARTER_LOOPS

_LOOPS = {loop["slug"]: loop for loop in STARTER_LOOPS}

# Known-good workspace fixtures that should make each loop's verification PASS.
_PASS_FIXTURES: dict[str, dict[str, str]] = {
    "hello-world-loop": {},
    "pr-review-loop": {},  # needs gh/$PR_NUMBER — exercised elsewhere; skip here
    "daily-briefing-loop": {},  # writes to /tmp; environment-dependent — skip run
    "test-green-loop": {"test_x.py": "def test_x():\n    assert 1 + 1 == 2\n"},
    "lint-clean-loop": {},  # ruff absent in CI -> exits 0 by design
    "secret-scan-loop": {"app.py": "print('hello')\n"},
    "changelog-from-commits-loop": {"CHANGELOG.md": "# Changelog\n## Added\n- a\n- b\n"},
    "doc-coverage-loop": {"target.py": 'def f():\n    "doc"\n    return 1\n'},
    "json-schema-validate-loop": {
        "schema.json": '{"required":["id"],"types":{"id":"int"}}',
        "data.json": '{"id": 1}',
    },
}

# Loops whose happy-path verification is deterministic in CI (no external tool /
# no /tmp side-channel). Only these get the pass-run assertion.
_RUNNABLE_PASS = [
    "hello-world-loop",
    "test-green-loop",
    "lint-clean-loop",
    "secret-scan-loop",
    "changelog-from-commits-loop",
    "doc-coverage-loop",
    "json-schema-validate-loop",
]

# Known-bad fixtures that MUST make verification FAIL (non-vacuous proof).
_FAIL_FIXTURES: dict[str, dict[str, str]] = {
    "test-green-loop": {"test_x.py": "def test_x():\n    assert 1 + 1 == 3\n"},
    "secret-scan-loop": {"leak.py": 'AWS = "AKIAIOSFODNN7EXAMPLE"\n'},
    "changelog-from-commits-loop": {"CHANGELOG.md": "\n\n"},
    "doc-coverage-loop": {"target.py": "def f():\n    return 1\n"},
    "json-schema-validate-loop": {
        "schema.json": '{"required":["id"],"types":{"id":"int"}}',
        "data.json": '{"id": "not-an-int"}',
    },
}


@pytest.fixture()
def runner(tmp_path):
    r = LoopRunner(workspace_base=str(tmp_path))
    r._backend = "none"  # force bounded mode (deterministic across hosts)
    return r


def test_every_seeded_loop_contract_validates():
    for slug, loop in _LOOPS.items():
        try:
            validate_loop_manifest(
                {
                    k: loop[k]
                    for k in (
                        "success_condition",
                        "verification_script",
                        "system_prompt",
                        "max_turns",
                        "budget_usd",
                        "tool_allowlist",
                        "stopping_criteria",
                    )
                }
            )
        except LoopValidationError as exc:  # pragma: no cover - failure path
            pytest.fail(f"seeded loop {slug!r} has an invalid contract: {exc}")


@pytest.mark.parametrize("slug", _RUNNABLE_PASS)
def test_seeded_loop_verification_passes_on_good_fixture(runner, slug):
    loop = _LOOPS[slug]
    res = runner.run_verification(
        loop_slug=slug,
        verification_script=loop["verification_script"],
        declared_bounds={},
        workspace_files=_PASS_FIXTURES.get(slug, {}),
    )
    assert res.passed is True, (
        f"{slug} verification should pass on good fixture; "
        f"exit={res.exit_code} stdout={res.stdout!r} stderr={res.stderr!r}"
    )


@pytest.mark.parametrize("slug", sorted(_FAIL_FIXTURES))
def test_seeded_loop_verification_fails_on_bad_fixture(runner, slug):
    loop = _LOOPS[slug]
    res = runner.run_verification(
        loop_slug=slug,
        verification_script=loop["verification_script"],
        declared_bounds={},
        workspace_files=_FAIL_FIXTURES[slug],
    )
    assert res.passed is False, (
        f"{slug} verification must FAIL on the bad fixture (non-vacuous); "
        f"exit={res.exit_code} stdout={res.stdout!r}"
    )
