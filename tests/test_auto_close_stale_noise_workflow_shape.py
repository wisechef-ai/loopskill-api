"""
Shape-only tests for .github/workflows/auto-close-stale-noise.yml
(repohygiene_2605 Phase G)

These assertions verify the workflow file is structurally correct without
simulating the full GitHub Actions runtime or the octokit API.
"""

import os
import re

import pytest
import yaml

WORKFLOW_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    ".github",
    "workflows",
    "auto-close-stale-noise.yml",
)


@pytest.fixture(scope="module")
def workflow_path() -> str:
    return os.path.abspath(WORKFLOW_PATH)


@pytest.fixture(scope="module")
def workflow_yaml(workflow_path: str) -> dict:
    with open(workflow_path) as fh:
        return yaml.safe_load(fh)


class TestAutoCloseStaleNoiseWorkflowShape:
    """Four structural assertions — file present, YAML valid, schedule present, hard cap present."""

    def test_file_exists(self, workflow_path: str) -> None:
        """Workflow YAML file must exist at the expected path."""
        assert os.path.isfile(workflow_path), (
            f"Workflow file not found at {workflow_path}"
        )

    def test_yaml_parses(self, workflow_yaml: dict) -> None:
        """Workflow file must be valid YAML and produce a non-empty dict."""
        assert isinstance(workflow_yaml, dict), "Parsed YAML is not a dict"
        assert workflow_yaml, "Parsed YAML is empty"

    def test_schedule_present(self, workflow_yaml: dict) -> None:
        """on.schedule must be defined (daily 06:00 UTC cron).

        Note: PyYAML parses bare ``on:`` as the boolean ``True``, so we look
        up both the boolean key and the string key for robustness.
        """
        # PyYAML maps bare `on:` → True (boolean), not the string "on"
        on_block = workflow_yaml.get(True) or workflow_yaml.get("on") or {}
        assert "schedule" in on_block, "on.schedule is missing from workflow"
        schedules = on_block["schedule"]
        assert isinstance(schedules, list) and len(schedules) >= 1, (
            "on.schedule must have at least one entry"
        )
        crons = [entry["cron"] for entry in schedules if "cron" in entry]
        assert len(crons) >= 1, "No cron expression found in on.schedule"
        assert crons[0] == "0 6 * * *", (
            f"Expected daily 06:00 UTC cron '0 6 * * *', got {crons[0]!r}"
        )

    def test_hard_cap_50_in_js_body(self, workflow_path: str) -> None:
        """The JS body must contain the HARD_CAP = 50 safety guard."""
        with open(workflow_path) as fh:
            raw = fh.read()
        # Allow for const HARD_CAP = 50 or HARD_CAP=50 or similar
        assert re.search(r"HARD_CAP\s*=\s*50", raw), (
            "Hard safety cap (HARD_CAP = 50) not found in workflow JS body"
        )
