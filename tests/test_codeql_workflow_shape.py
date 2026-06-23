"""Shape-test for .github/workflows/codeql.yml.

Assertions:
1. File exists at the expected path.
2. YAML parses cleanly (no syntax errors).
3. on.schedule is present AND on.push is NOT present (enforces 'cheap on push' contract).
"""

import pathlib

import yaml


WORKFLOW_PATH = pathlib.Path(__file__).parent.parent / ".github" / "workflows" / "codeql.yml"


def test_codeql_workflow_file_exists():
    """Assertion 1: the workflow file exists."""
    assert WORKFLOW_PATH.exists(), f"Expected codeql.yml at {WORKFLOW_PATH}"


def test_codeql_workflow_yaml_parses_cleanly():
    """Assertion 2: the YAML is syntactically valid."""
    content = WORKFLOW_PATH.read_text()
    parsed = yaml.safe_load(content)
    assert isinstance(parsed, dict), "Expected a non-empty YAML mapping"


def test_codeql_workflow_schedule_present_push_absent():
    """Assertion 3: on.schedule is present AND on.push is NOT present.

    Note: PyYAML parses the bare YAML key ``on`` as boolean ``True`` (YAML 1.1
    compatibility).  The workflow uses the canonical unquoted form that matches
    the rest of the repo, so we look up ``True`` here.
    """
    content = WORKFLOW_PATH.read_text()
    parsed = yaml.safe_load(content)
    # PyYAML maps bare `on:` → True (YAML 1.1 bool); use True as the key.
    triggers = parsed.get(True, {})
    assert "schedule" in triggers, "Expected on.schedule to be present (weekly run)"
    assert "push" not in triggers, (
        "on.push must NOT be present — main pushes stay cheap per repohygiene_2605 §D step 2"
    )
