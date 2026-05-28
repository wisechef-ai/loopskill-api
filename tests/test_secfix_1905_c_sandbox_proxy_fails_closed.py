"""Tests for Issue #8 — Network proxy fails CLOSED.

TDD structure:
  test_pov_* — proof-of-vulnerability: passes on broken code, shows the exploit.
  test_proxy_failure_fails_closed — regression: fails on broken code, passes after fix.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from app.sandbox.profile import SandboxProfile
from app.sandbox.runner import SandboxResult, SandboxRunner

pytestmark = [pytest.mark.sandbox_linux_only]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner(tmp_path):
    """Return a SandboxRunner with firejail as backend (mocked)."""
    runner = SandboxRunner(workspace=str(tmp_path / "work"))
    runner._backend = "firejail"
    return runner


def _make_skill_dir(tmp_path):
    """Create a minimal skill directory."""
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    ep = skill_dir / "setup.sh"
    ep.write_text("#!/bin/bash\necho ok\n")
    return skill_dir


def _run_with_failing_proxy(runner, skill_dir, profile):
    """Execute runner.run with _start_domain_proxy_sync mocked to raise."""
    with patch.object(
        runner.__class__,
        "_start_domain_proxy_sync",
        side_effect=RuntimeError("proxy start failed"),
    ):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=b"ok\n", stderr=b""
            )
            with patch("shutil.copytree"), patch("os.chmod"):
                return runner.run(str(skill_dir), "setup.sh", profile)


# ---------------------------------------------------------------------------
# PROOF OF VULNERABILITY (#8) — These tests pass on UNFIXED code and
# document the original vulnerable behaviour.
# ---------------------------------------------------------------------------

def test_pov_proxy_failure_falls_back_to_unrestricted(tmp_path, caplog):
    """POV #8: Before fix, proxy exception silently falls back to unrestricted.

    On broken code the sandbox runs with unrestricted network and returns
    exit_code=0; the log contains 'Falling back'.  After the fix this test
    will fail (exit_code becomes -1) — that is intentional and expected.
    """
    runner = _make_runner(tmp_path)
    skill_dir = _make_skill_dir(tmp_path)
    profile = SandboxProfile(network_allow=["api.github.com"])

    with caplog.at_level(logging.WARNING, logger="app.sandbox.runner"):
        result = _run_with_failing_proxy(runner, skill_dir, profile)

    # VULNERABILITY: unrestricted execution proceeds even though proxy failed.
    # On the pre-fix codebase this assertion passes (proving the bug).
    # After the fix, exit_code becomes -1 and error='proxy_failed'.
    if result.error == "proxy_failed":
        pytest.skip("Fix already applied — PoV no longer demonstrates the bug")
    assert result.exit_code == 0, (
        "Pre-fix: expected sandbox to fall back to unrestricted (exit_code 0)"
    )
    assert any("Falling back" in r.message for r in caplog.records), (
        "Pre-fix: expected 'Falling back' in warning log"
    )


# ---------------------------------------------------------------------------
# REGRESSION TESTS — Fail on broken code, pass after fix.
# ---------------------------------------------------------------------------

def test_proxy_failure_fails_closed(tmp_path):
    """Issue #8 fix: proxy startup exception → SandboxResult with error='proxy_failed', exit_code=-1."""
    runner = _make_runner(tmp_path)
    skill_dir = _make_skill_dir(tmp_path)
    profile = SandboxProfile(network_allow=["api.github.com"])

    result = _run_with_failing_proxy(runner, skill_dir, profile)

    assert result.exit_code == -1, f"Expected -1, got {result.exit_code}"
    assert result.error == "proxy_failed", f"Expected 'proxy_failed', got {result.error!r}"
    assert "proxy could not start" in result.stderr.lower(), (
        f"Expected 'proxy could not start' in stderr, got: {result.stderr!r}"
    )


def test_proxy_failure_bwrap_fails_closed(tmp_path):
    """Issue #8 fix: bwrap path also fails closed when proxy raises."""
    runner = _make_runner(tmp_path)
    runner._backend = "bwrap"
    skill_dir = _make_skill_dir(tmp_path)
    profile = SandboxProfile(network_allow=["api.github.com"])

    result = _run_with_failing_proxy(runner, skill_dir, profile)

    assert result.exit_code == -1
    assert result.error == "proxy_failed"


def test_proxy_success_proceeds_normally(tmp_path):
    """Sanity: when proxy starts fine, execution is not blocked."""
    runner = _make_runner(tmp_path)
    skill_dir = _make_skill_dir(tmp_path)
    profile = SandboxProfile(network_allow=["api.github.com"])

    mock_proxy = {"process": MagicMock(), "port": 9999}
    with patch.object(
        runner.__class__,
        "_start_domain_proxy_sync",
        return_value=mock_proxy,
    ):
        with patch.object(runner.__class__, "_stop_domain_proxy_sync"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=0, stdout=b"ok\n", stderr=b""
                )
                with patch("shutil.copytree"), patch("os.chmod"):
                    result = runner.run(str(skill_dir), "setup.sh", profile)

    assert result.exit_code == 0
    assert result.error is None


def test_no_network_allow_skips_proxy(tmp_path):
    """Sanity: when network_allow is empty, proxy is never started."""
    runner = _make_runner(tmp_path)
    skill_dir = _make_skill_dir(tmp_path)
    profile = SandboxProfile(network_allow=[])  # no network

    with patch.object(
        runner.__class__,
        "_start_domain_proxy_sync",
        side_effect=AssertionError("Should not be called"),
    ):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"ok\n", stderr=b"")
            with patch("shutil.copytree"), patch("os.chmod"):
                result = runner.run(str(skill_dir), "setup.sh", profile)

    assert result.exit_code == 0
