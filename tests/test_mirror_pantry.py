"""tests/test_mirror_pantry.py

Tests for scripts/mirror_pantry.py — validates the 3 upstream repo cloning
logic with mocked GitHub API responses. No actual network calls.
"""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.mirror_pantry import (
    check_license,
    clone_repo,
    get_repo_sha,
    PANTRY_SOURCES,
    LicenseError,
)


# ── PANTRY_SOURCES constant ──────────────────────────────────────────────

class TestPantrySourcesConstant:
    def test_exactly_three_sources(self):
        assert len(PANTRY_SOURCES) == 3

    def test_obra_superpowers_present(self):
        slugs = [s["repo"] for s in PANTRY_SOURCES]
        assert "obra/superpowers" in slugs

    def test_houseofmvps_ultraship_present(self):
        slugs = [s["repo"] for s in PANTRY_SOURCES]
        assert "Houseofmvps/ultraship" in slugs

    def test_wisechef_awesome_present(self):
        slugs = [s["repo"] for s in PANTRY_SOURCES]
        assert "wisechef-ai/awesome-agent-recipes" in slugs

    def test_each_source_has_expected_keys(self):
        for src in PANTRY_SOURCES:
            assert "repo" in src
            assert "license" in src

    def test_all_licenses_are_permitted(self):
        permitted = {"MIT", "Apache-2.0"}
        for src in PANTRY_SOURCES:
            assert src["license"] in permitted


# ── check_license ────────────────────────────────────────────────────────

class TestCheckLicense:
    def _make_gh_response(self, spdx_id: str) -> str:
        return json.dumps({"license": {"spdx_id": spdx_id}})

    @patch("subprocess.run")
    def test_mit_license_passes(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._make_gh_response("MIT"),
        )
        check_license("obra/superpowers", "MIT")  # should not raise

    @patch("subprocess.run")
    def test_apache_license_passes(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._make_gh_response("Apache-2.0"),
        )
        check_license("some/repo", "Apache-2.0")  # should not raise

    @patch("subprocess.run")
    def test_gpl_license_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._make_gh_response("GPL-3.0"),
        )
        with pytest.raises(LicenseError):
            check_license("some/repo", "MIT")

    @patch("subprocess.run")
    def test_license_mismatch_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=self._make_gh_response("Apache-2.0"),
        )
        with pytest.raises(LicenseError):
            check_license("some/repo", "MIT")

    @patch("subprocess.run")
    def test_gh_api_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        with pytest.raises(RuntimeError):
            check_license("some/repo", "MIT")


# ── get_repo_sha ─────────────────────────────────────────────────────────

class TestGetRepoSha:
    @patch("subprocess.run")
    def test_returns_sha_from_gh_api(self, mock_run):
        payload = json.dumps({"sha": "abc123def456"})
        mock_run.return_value = MagicMock(returncode=0, stdout=payload)
        sha = get_repo_sha("obra/superpowers")
        assert sha == "abc123def456"

    @patch("subprocess.run")
    def test_sha_is_non_empty_string(self, mock_run):
        payload = json.dumps({"sha": "deadbeef" * 8})
        mock_run.return_value = MagicMock(returncode=0, stdout=payload)
        sha = get_repo_sha("obra/superpowers")
        assert isinstance(sha, str)
        assert len(sha) > 0

    @patch("subprocess.run")
    def test_failure_raises_runtime_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        with pytest.raises(RuntimeError):
            get_repo_sha("bad/repo")


# ── clone_repo ───────────────────────────────────────────────────────────

class TestCloneRepo:
    @patch("subprocess.run")
    @patch("scripts.mirror_pantry.check_license")
    @patch("scripts.mirror_pantry.get_repo_sha")
    def test_clone_calls_gh_repo_clone(self, mock_sha, mock_license, mock_run):
        mock_sha.return_value = "abc123"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_repo("obra/superpowers", "MIT", dest=tmpdir)
        mock_run.assert_called()

    @patch("subprocess.run")
    @patch("scripts.mirror_pantry.check_license")
    @patch("scripts.mirror_pantry.get_repo_sha")
    def test_clone_returns_sha_info(self, mock_sha, mock_license, mock_run):
        mock_sha.return_value = "deadbeef123"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            result = clone_repo("obra/superpowers", "MIT", dest=tmpdir)
        assert result["sha"] == "deadbeef123"
        assert result["repo"] == "obra/superpowers"

    @patch("subprocess.run")
    @patch("scripts.mirror_pantry.check_license")
    @patch("scripts.mirror_pantry.get_repo_sha")
    def test_license_check_is_called_before_clone(self, mock_sha, mock_license, mock_run):
        mock_sha.return_value = "abc123"
        mock_run.return_value = MagicMock(returncode=0)
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_repo("obra/superpowers", "MIT", dest=tmpdir)
        mock_license.assert_called_once_with("obra/superpowers", "MIT")
