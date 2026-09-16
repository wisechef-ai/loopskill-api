"""Runner-bug regression (coldstart_0609, run 20260916-84de0531): the parent
Hermes on the eval box switched its model provider to `zai`, but the runner's
credential plumbing only knew anthropic/openai/copilot. The isolated HERMES_HOME
was written with zero usable credentials, so every hermes leg was stillborn
(`No usable credentials found for provider 'zai'`, 7s, 0 tool calls) and — worse
— was scored `fail`, charging a harness artifact against the product. Per
RUBRIC.md that is outcome `error` (excluded), never `fail`.

Three contracts pinned here:
1. `zai` is a mapped provider (key vars GLM/ZAI/Z_AI, base-url var GLM_BASE_URL
   — mirroring hermes_cli.providers' zai overlay).
2. `write_isolated_hermes_home` copies the provider key AND base_url into the
   isolated home (the parent uses a non-default coding endpoint), while never
   copying LoopSkill key material.
3. A stillborn hermes run (credentials never found) surfaces as a harness
   error, so `run_task` scores it `error`, not `fail`.

The hermes binary is faked on PATH so nothing touches a real provider account.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "evals" / "agent_coldstart"))

import run as run_mod  # noqa: E402


def _synthetic_zai_parent(tmp_path: Path) -> Path:
    """A parent ~/.hermes shaped like the real eval box: zai provider with a
    coding-endpoint base_url, a provider key in .env, and LoopSkill key
    material that must NEVER migrate into the isolated home."""
    parent = tmp_path / "parent-hermes"
    parent.mkdir(parents=True, exist_ok=True)
    (parent / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "glm-5.3-flash",
                    "provider": "zai",
                    "base_url": "https://api.z.ai/api/coding/paas/v4",
                }
            }
        )
    )
    (parent / ".env").write_text(
        "GLM_API_KEY=fake-glm-key-0000000000000001\n"
        "GLM_BASE_URL=https://api.z.ai/api/coding/paas/v4\n"
        "LOOPSKILL_API_KEY=rec_live_should_not_migrate\n"
    )
    return parent


def test_zai_provider_is_mapped():
    assert "zai" in run_mod.PROVIDER_CRED_VARS
    for var in ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"):
        assert var in run_mod.PROVIDER_CRED_VARS["zai"]
    assert run_mod.PROVIDER_BASE_URL_VARS.get("zai") == "GLM_BASE_URL"


def test_isolated_home_carries_zai_creds_and_base_url(tmp_path):
    parent = _synthetic_zai_parent(tmp_path)
    home = tmp_path / "cold-home"
    home.mkdir()
    hermes_home, model = run_mod.write_isolated_hermes_home(home, parent)

    assert model == "glm-5.3-flash"
    isolated_cfg = yaml.safe_load((hermes_home / "config.yaml").read_text())
    assert isolated_cfg["model"]["provider"] == "zai"
    # base_url must ride along or the isolated agent hits the default endpoint
    assert isolated_cfg["model"].get("base_url") == "https://api.z.ai/api/coding/paas/v4"

    isolated_env = (hermes_home / ".env").read_text()
    assert "fake-glm-key-0000000000000001" in isolated_env
    assert "GLM_BASE_URL=https://api.z.ai/api/coding/paas/v4" in isolated_env
    # blindness contract: LoopSkill key material never migrates
    assert "rec_live_should_not_migrate" not in isolated_env

    # and the copied credential set itself must pass the isolation scan
    run_mod.assert_no_secret_leak(home)


@pytest.fixture
def fake_hermes_path(tmp_path, monkeypatch):
    """Fake `hermes` binary reproducing the observed stillbirth: prints the
    credentials-not-found banner and exits 1 without doing any work."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "hermes"
    script.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"No usable credentials found for provider 'zai'."
        ' Set GLM_API_KEY, ZAI_API_KEY, Z_AI_API_KEY."\n'
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # keep the real ~/.hermes out of reach: parent home is synthetic below
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    (tmp_path / "nohome").mkdir()
    return bin_dir


def test_stillborn_hermes_is_harness_error_not_fail(fake_hermes_path, tmp_path):
    parent = _synthetic_zai_parent(tmp_path)
    res = run_mod.run_hermes_harness(
        "do the task", tmp_path / "cold-home", max_minutes=1, parent_hermes_home=parent
    )
    assert res.error is not None, "stillbirth must surface as a harness error, not a silent fail"
    assert "No usable credentials" in res.error
    assert res.timed_out is False


def test_stillborn_hermes_run_task_scores_error(fake_hermes_path, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(_synthetic_zai_parent(tmp_path)))
    results = tmp_path / "results"
    res = run_mod.run_task(
        harness="hermes",
        task_id="report-skill-error",
        results_dir=results,
        parent_hermes_home=_synthetic_zai_parent(tmp_path),
        run_id="deadbef0",
    )
    assert res.outcome == "error", (
        f"harness stillbirth scored {res.outcome!r} — must be excluded per RUBRIC, not charged to the product"
    )
    row = json.loads((results / "hermes_report-skill-error_deadbef0.json").read_text())
    assert row["outcome"] == "error"
    assert "No usable credentials" in (row["check_tail"] or "")
