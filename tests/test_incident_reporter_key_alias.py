"""Regression test for issue #219 item 1 — LOOPSKILL_API_KEY alias.

recipes-auto-improve/incident_reporter.py's CLI only read RECIPES_API_KEY
from the environment. Docs and MCP config across the portal use the
LOOPSKILL_API_KEY name (product is branded LoopSkill everywhere else); a
user who set only LOOPSKILL_API_KEY got a silent "needs RECIPES_API_KEY"
usage error even though their key was perfectly valid.

Precedent: tools/recipes_cli.py already implements this exact dual-accept
pattern (LOOPSKILL_API_KEY canonical, RECIPES_API_KEY fallback, prefer the
new name when both are set) — see qa0208-w3. This test locks the same
contract onto incident_reporter.py's CLI.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

MODULE_PATH = (
    pathlib.Path(__file__).parent.parent
    / "meta-skills"
    / "recipes-auto-improve"
    / "incident_reporter.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("incident_reporter_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def reporter(monkeypatch):
    # Force a fresh import each test — the module reads os.environ at
    # argparse-definition time (module-level defaults), so a cached import
    # would leak state between tests.
    for name in list(sys.modules):
        if name == "incident_reporter_under_test":
            del sys.modules[name]
    return _load_module()


def test_loopskill_api_key_alone_is_accepted(monkeypatch, reporter):
    """A user who only exported LOOPSKILL_API_KEY must not be told to set
    RECIPES_API_KEY — that's the exact confusion issue #219 documents."""
    monkeypatch.delenv("RECIPES_API_KEY", raising=False)
    monkeypatch.setenv("LOOPSKILL_API_KEY", "loop_live_test_key")
    monkeypatch.setenv("RECIPES_AGENT_FP", "fp123")
    reporter = _load_module()  # re-import AFTER env is set (module-level defaults)

    args = reporter._parse_key_args(["--skill-id", "abc", "--", "echo", "hi"])
    assert args.api_key == "loop_live_test_key"


def test_loopskill_api_key_preferred_over_legacy_when_both_set(monkeypatch):
    monkeypatch.setenv("LOOPSKILL_API_KEY", "loop_live_new")
    monkeypatch.setenv("RECIPES_API_KEY", "rec_live_old")
    monkeypatch.setenv("RECIPES_AGENT_FP", "fp123")
    reporter = _load_module()

    args = reporter._parse_key_args(["--skill-id", "abc", "--", "echo", "hi"])
    assert args.api_key == "loop_live_new"


def test_legacy_recipes_api_key_still_works_as_fallback(monkeypatch):
    monkeypatch.delenv("LOOPSKILL_API_KEY", raising=False)
    monkeypatch.setenv("RECIPES_API_KEY", "rec_live_legacy_only")
    monkeypatch.setenv("RECIPES_AGENT_FP", "fp123")
    reporter = _load_module()

    args = reporter._parse_key_args(["--skill-id", "abc", "--", "echo", "hi"])
    assert args.api_key == "rec_live_legacy_only"
