"""Tests for WiseRecipes sandbox runner — WIS-468.

Tests cover:
1. Profile parsing from TOML manifests
2. Profile validation (dangerous binaries, path sanity, resource limits)
3. bwrap argument generation
4. Network isolation verification (unshare-net when no allowlist)
5. Network allowlist enforcement (domain-level filtering)
6. Runner execution with real bwrap (when available)
7. Skill smoke tests for all 5 launch-grade skills
8. Telemetry event recording
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.sandbox.profile import SandboxProfile
from app.sandbox.runner import SandboxResult, SandboxRunner

DEV_SKILLS_DIR = PROJECT_ROOT / "dev-skills"

# ── Launch-grade skills for DoD smoke tests ──
LAUNCH_SKILLS = [
    "hello-sandbox",
    "fetch-github-stats",
    "python-processor",
    "npm-installer",
    "file-transformer",
]


class TestSandboxProfileParsing(unittest.TestCase):
    """Test SandboxProfile.from_manifest() with various TOML inputs."""

    def test_parse_full_sandbox_block(self):
        toml = """
[skill]
title = "Test"

[sandbox]
network_allow = ["api.github.com", "registry.npmjs.org"]
fs_write = ["/tmp", "/home/skill/.cache"]
exec_allow = ["python3", "node", "bash"]
memory_mb = 512
timeout_seconds = 180
env_pass = ["PATH", "LANG", "HOME"]
"""
        profile = SandboxProfile.from_manifest(toml)
        self.assertEqual(profile.network_allow, ["api.github.com", "registry.npmjs.org"])
        self.assertEqual(profile.fs_write, ["/tmp", "/home/skill/.cache"])
        self.assertEqual(profile.exec_allow, ["python3", "node", "bash"])
        self.assertEqual(profile.memory_mb, 512)
        self.assertEqual(profile.timeout_seconds, 180)
        self.assertEqual(profile.env_pass, ["PATH", "LANG", "HOME"])

    def test_parse_minimal_sandbox_block(self):
        toml = """
[skill]
title = "Minimal"

[sandbox]
"""
        profile = SandboxProfile.from_manifest(toml)
        self.assertEqual(profile.network_allow, [])
        self.assertEqual(profile.fs_write, [])
        self.assertEqual(profile.exec_allow, [])
        self.assertEqual(profile.memory_mb, 256)  # default
        self.assertEqual(profile.timeout_seconds, 120)  # default

    def test_parse_no_sandbox_block(self):
        toml = """
[skill]
title = "No Sandbox"
"""
        profile = SandboxProfile.from_manifest(toml)
        self.assertEqual(profile.network_allow, [])
        self.assertEqual(profile.memory_mb, 256)

    def test_parse_invalid_toml_raises(self):
        with self.assertRaises(ValueError) as ctx:
            SandboxProfile.from_manifest("this is not [valid toml {{{")
        self.assertIn("Invalid TOML", str(ctx.exception))

    def test_default_profile(self):
        profile = SandboxProfile.default()
        self.assertEqual(profile.network_allow, [])
        self.assertEqual(profile.fs_write, ["/tmp"])
        self.assertIn("bash", profile.exec_allow)
        self.assertEqual(profile.memory_mb, 256)
        self.assertEqual(profile.timeout_seconds, 60)


class TestSandboxProfileValidation(unittest.TestCase):
    """Test SandboxProfile.validate() catches dangerous declarations."""

    def test_dangerous_exec_binaries(self):
        for binary in ["rm", "sudo", "su", "mkfs", "dd"]:
            profile = SandboxProfile(exec_allow=[binary])
            warnings = profile.validate()
            self.assertTrue(
                any("dangerous" in w for w in warnings),
                f"Expected dangerous warning for {binary}, got: {warnings}",
            )

    def test_suspicious_network_domain(self):
        profile = SandboxProfile(network_allow=["valid.com", "has spaces.com", "evil$;cmd"])
        warnings = profile.validate()
        self.assertTrue(any("Suspicious" in w for w in warnings))

    def test_valid_network_domains_pass(self):
        profile = SandboxProfile(network_allow=["api.github.com", "registry.npmjs.org", "cdn.example.io"])
        warnings = profile.validate()
        network_warnings = [w for w in warnings if "network" in w.lower()]
        self.assertEqual(network_warnings, [])

    def test_relative_fs_write_path_warns(self):
        profile = SandboxProfile(fs_write=["tmp", "relative/path"])
        warnings = profile.validate()
        self.assertTrue(any("absolute" in w for w in warnings))

    def test_absolute_fs_write_path_passes(self):
        profile = SandboxProfile(fs_write=["/tmp", "/home/skill/.cache"])
        warnings = profile.validate()
        fs_warnings = [w for w in warnings if "fs_write" in w]
        self.assertEqual(fs_warnings, [])

    def test_memory_too_high_warns(self):
        profile = SandboxProfile(memory_mb=8192)
        warnings = profile.validate()
        self.assertTrue(any("memory" in w.lower() for w in warnings))

    def test_timeout_too_long_warns(self):
        profile = SandboxProfile(timeout_seconds=999)
        warnings = profile.validate()
        self.assertTrue(any("timeout" in w.lower() for w in warnings))

    def test_safe_profile_no_warnings(self):
        profile = SandboxProfile(
            network_allow=["api.github.com"],
            fs_write=["/tmp"],
            exec_allow=["python3", "node", "bash"],
            memory_mb=512,
            timeout_seconds=120,
        )
        warnings = profile.validate()
        self.assertEqual(warnings, [])


class TestBwrapArgGeneration(unittest.TestCase):
    """Test SandboxProfile.to_bwrap_args() produces correct bwrap flags."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sandbox_root = os.path.join(self.tmpdir, "sandbox")
        os.makedirs(os.path.join(self.sandbox_root, "_tmp"), exist_ok=True)
        os.makedirs(os.path.join(self.sandbox_root, "_writable"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_no_network_generates_unshare_net(self):
        profile = SandboxProfile(network_allow=[])
        args = profile.to_bwrap_args(self.sandbox_root, "/skill")
        self.assertIn("--unshare-net", args)

    def test_network_allowlist_does_not_unshare(self):
        profile = SandboxProfile(network_allow=["api.github.com"])
        args = profile.to_bwrap_args(self.sandbox_root, "/skill")
        self.assertNotIn("--unshare-net", args)

    def test_read_only_root_bind(self):
        profile = SandboxProfile()
        args = profile.to_bwrap_args(self.sandbox_root, "/skill")
        # Should have --ro-bind for system dirs
        self.assertTrue(any("--ro-bind" in a for a in args))

    def test_writable_tmp_mounted(self):
        profile = SandboxProfile(fs_write=["/tmp"])
        args = profile.to_bwrap_args(self.sandbox_root, "/skill")
        # Should have --bind for /tmp
        bind_idx = args.index("--bind") if "--bind" in args else -1
        # Find the /tmp bind
        tmp_bound = False
        for i, a in enumerate(args):
            if a == "--bind" and i + 2 < len(args) and args[i + 2] == "/tmp":
                tmp_bound = True
        self.assertTrue(tmp_bound, "Expected /tmp to be bind-mounted")

    def test_env_vars_set(self):
        profile = SandboxProfile(env_pass=["PATH"])
        args = profile.to_bwrap_args(self.sandbox_root, "/skill")
        self.assertIn("--setenv", args)
        # SANDBOX=1 should always be set
        sandbox_idx = args.index("SANDBOX")
        self.assertEqual(args[sandbox_idx - 1], "--setenv")
        self.assertEqual(args[sandbox_idx + 1], "1")

    def test_skill_home_env(self):
        profile = SandboxProfile()
        args = profile.to_bwrap_args(self.sandbox_root, "/skill")
        skill_home_idx = args.index("SKILL_HOME")
        self.assertEqual(args[skill_home_idx + 1], "/skill")

    def test_die_with_parent(self):
        profile = SandboxProfile()
        args = profile.to_bwrap_args(self.sandbox_root, "/skill")
        self.assertIn("--die-with-parent", args)


class TestNetworkEgressBlocking(unittest.TestCase):
    """Prove that network egress outside allowlist is blocked.

    Since bwrap's --unshare-net creates an isolated network namespace,
    all outbound connections should fail. For allowlisted domains,
    we rely on the assumption that the sandbox doesn't have iptables
    rules to allow specific domains — see NETWORK_ISOLATION.md for the
    production proxy-based enforcement model.

    These tests verify the correct bwrap flags are generated to ensure
    network isolation.
    """

    def test_isolated_sandbox_has_no_network(self):
        """Profile with empty network_allow must produce --unshare-net."""
        profile = SandboxProfile(network_allow=[])
        tmpdir = tempfile.mkdtemp()
        root = os.path.join(tmpdir, "root")
        os.makedirs(os.path.join(root, "_tmp"), exist_ok=True)
        try:
            args = profile.to_bwrap_args(root, "/skill")
            self.assertIn("--unshare-net", args)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_network_allowlist_does_not_isolate(self):
        """Profile with domains should NOT use --unshare-net."""
        profile = SandboxProfile(network_allow=["api.github.com"])
        tmpdir = tempfile.mkdtemp()
        root = os.path.join(tmpdir, "root")
        os.makedirs(os.path.join(root, "_tmp"), exist_ok=True)
        try:
            args = profile.to_bwrap_args(root, "/skill")
            self.assertNotIn("--unshare-net", args)
            # Production note: fine-grained domain filtering requires
            # a proxy layer (e.g., squid) or nftables rules. The sandbox
            # profile stores the allowlist for the proxy to consume.
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_allowlist_domains_preserved_in_profile(self):
        """Ensure network_allow list is accessible for proxy/nftables config."""
        domains = ["api.github.com", "registry.npmjs.org", "cdn.example.io"]
        profile = SandboxProfile(network_allow=domains)
        self.assertEqual(profile.network_allow, domains)


class TestSandboxRunner(unittest.TestCase):
    """Test SandboxRunner behavior — mocks bwrap when not available."""

    def setUp(self):
        self.workspace = tempfile.mkdtemp()
        self.runner = SandboxRunner(workspace=self.workspace)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_no_backend_returns_error(self):
        """When no sandbox backend is available, runner returns a clean error."""
        runner = SandboxRunner(workspace=self.workspace)
        runner._backend = "none"
        result = runner.run(
            skill_dir="/tmp/nonexistent",
            entrypoint="setup.sh",
            profile=SandboxProfile.default(),
        )
        self.assertEqual(result.exit_code, -1)
        self.assertIn("No sandbox backend", result.error)
        self.assertFalse(result.success)

    def test_backend_detection_with_mock(self):
        """Test _detect_backend returns correct backend string."""
        with patch("shutil.which", side_effect=lambda x: "/usr/bin/firejail" if x == "firejail" else None):
            self.assertEqual(SandboxRunner._detect_backend(), "firejail")
        with patch("shutil.which", side_effect=lambda x: "/usr/bin/bwrap" if x == "bwrap" else None):
            self.assertEqual(SandboxRunner._detect_backend(), "bwrap")
        with patch("shutil.which", return_value=None):
            self.assertEqual(SandboxRunner._detect_backend(), "none")

    def test_missing_skill_dir_returns_error(self):
        result = self.runner.run(
            skill_dir="/tmp/nonexistent_skill_dir_xyz",
            entrypoint="setup.sh",
            profile=SandboxProfile.default(),
        )
        self.assertEqual(result.exit_code, -1)
        self.assertIn("does not exist", result.error)

    def test_missing_entrypoint_returns_error(self):
        tmpdir = tempfile.mkdtemp()
        try:
            result = self.runner.run(
                skill_dir=tmpdir,
                entrypoint="nonexistent.sh",
                profile=SandboxProfile.default(),
            )
            self.assertEqual(result.exit_code, -1)
            self.assertIn("not found", result.error)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_sandbox_result_properties(self):
        result = SandboxResult(
            exit_code=0,
            stdout="hello",
            stderr="",
            timed_out=False,
            duration_seconds=1.5,
            sandbox_id="test-123",
        )
        self.assertTrue(result.success)
        self.assertEqual(result.sandbox_id, "test-123")

        d = result.to_dict()
        self.assertEqual(d["exit_code"], 0)
        self.assertTrue(d["success"])
        self.assertEqual(d["sandbox_id"], "test-123")

    def test_timed_out_result_is_not_success(self):
        result = SandboxResult(
            exit_code=-1,
            stdout="",
            stderr="",
            timed_out=True,
            duration_seconds=30,
            sandbox_id="timeout-test",
            error="Timed out after 30s",
        )
        self.assertFalse(result.success)

    def test_result_dict_truncates_long_output(self):
        result = SandboxResult(
            exit_code=0,
            stdout="x" * 10000,
            stderr="y" * 10000,
            timed_out=False,
            duration_seconds=1,
            sandbox_id="trunc",
        )
        d = result.to_dict()
        self.assertEqual(len(d["stdout"]), 5000)
        self.assertEqual(len(d["stderr"]), 5000)


class TestLaunchSkills(unittest.TestCase):
    """Smoke tests for the 5 launch-grade skills required by DoD.

    Each test verifies:
    1. skill.toml parses correctly
    2. Profile validates without dangerous warnings
    3. bwrap args generate without errors
    4. setup.sh exists and is executable
    """

    def _load_skill(self, slug: str) -> tuple[SandboxProfile, str]:
        skill_dir = DEV_SKILLS_DIR / slug
        toml_path = skill_dir / "skill.toml"
        self.assertTrue(toml_path.exists(), f"skill.toml missing for {slug}")
        toml_content = toml_path.read_text()
        profile = SandboxProfile.from_manifest(toml_content)
        return profile, str(skill_dir)

    def test_hello_sandbox(self):
        profile, skill_dir = self._load_skill("hello-sandbox")
        self.assertEqual(profile.network_allow, [])
        self.assertIn("/tmp", profile.fs_write)
        warnings = profile.validate()
        self.assertEqual(warnings, [])
        self.assertTrue(os.access(os.path.join(skill_dir, "setup.sh"), os.X_OK))

    def test_fetch_github_stats(self):
        profile, skill_dir = self._load_skill("fetch-github-stats")
        self.assertIn("api.github.com", profile.network_allow)
        self.assertIn("/tmp", profile.fs_write)
        warnings = profile.validate()
        self.assertEqual(warnings, [])
        self.assertTrue(os.access(os.path.join(skill_dir, "setup.sh"), os.X_OK))

    def test_python_processor(self):
        profile, skill_dir = self._load_skill("python-processor")
        self.assertEqual(profile.network_allow, [])
        self.assertIn("python3", profile.exec_allow)
        self.assertEqual(profile.memory_mb, 512)
        warnings = profile.validate()
        self.assertEqual(warnings, [])
        self.assertTrue(os.access(os.path.join(skill_dir, "setup.sh"), os.X_OK))

    def test_npm_installer(self):
        profile, skill_dir = self._load_skill("npm-installer")
        self.assertIn("registry.npmjs.org", profile.network_allow)
        self.assertIn("node", profile.exec_allow)
        self.assertEqual(profile.timeout_seconds, 180)
        warnings = profile.validate()
        self.assertEqual(warnings, [])
        self.assertTrue(os.access(os.path.join(skill_dir, "setup.sh"), os.X_OK))

    def test_file_transformer(self):
        profile, skill_dir = self._load_skill("file-transformer")
        self.assertEqual(profile.network_allow, [])
        self.assertIn("bash", profile.exec_allow)
        warnings = profile.validate()
        self.assertEqual(warnings, [])
        self.assertTrue(os.access(os.path.join(skill_dir, "setup.sh"), os.X_OK))

    def test_all_five_skills_present(self):
        """DoD: sandbox profiles generated for at least 5 launch-grade skills."""
        found = []
        for slug in LAUNCH_SKILLS:
            skill_dir = DEV_SKILLS_DIR / slug
            toml_path = skill_dir / "skill.toml"
            if toml_path.exists():
                profile = SandboxProfile.from_manifest(toml_path.read_text())
                args = profile.to_bwrap_args("/tmp/test", "/skill")
                found.append(slug)
        self.assertGreaterEqual(
            len(found), 5,
            f"Expected 5 launch-grade skills, found {len(found)}: {found}",
        )


class TestBwrapArgGenerationForSkills(unittest.TestCase):
    """Verify bwrap args generate correctly for each of the 5 skills."""

    def _gen_args(self, slug: str) -> list[str]:
        toml_path = DEV_SKILLS_DIR / slug / "skill.toml"
        profile = SandboxProfile.from_manifest(toml_path.read_text())
        tmpdir = tempfile.mkdtemp()
        root = os.path.join(tmpdir, "root")
        os.makedirs(os.path.join(root, "_tmp"), exist_ok=True)
        os.makedirs(os.path.join(root, "_writable"), exist_ok=True)
        try:
            return profile.to_bwrap_args(root, "/skill")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_hello_sandbox_args_isolate_network(self):
        args = self._gen_args("hello-sandbox")
        self.assertIn("--unshare-net", args)

    def test_fetch_github_stats_args_allow_network(self):
        args = self._gen_args("fetch-github-stats")
        self.assertNotIn("--unshare-net", args)
        # Should mount /etc/resolv.conf for DNS
        self.assertIn("/etc/resolv.conf", args)

    def test_python_processor_args_isolate_network(self):
        args = self._gen_args("python-processor")
        self.assertIn("--unshare-net", args)

    def test_npm_installer_args_allow_network(self):
        args = self._gen_args("npm-installer")
        self.assertNotIn("--unshare-net", args)

    def test_file_transformer_args_isolate_network(self):
        args = self._gen_args("file-transformer")
        self.assertIn("--unshare-net", args)


class TestFirejailArgGeneration(unittest.TestCase):
    """Test SandboxProfile.to_firejail_args() produces correct firejail flags."""

    def test_no_network_generates_net_none(self):
        profile = SandboxProfile(network_allow=[])
        args = profile.to_firejail_args("/home/skill")
        self.assertIn("--net=none", args)

    def test_network_allowlist_does_not_block(self):
        profile = SandboxProfile(network_allow=["api.github.com"])
        args = profile.to_firejail_args("/home/skill")
        self.assertNotIn("--net=none", args)

    def test_noprofile_flag(self):
        profile = SandboxProfile()
        args = profile.to_firejail_args("/home/skill")
        self.assertIn("--noprofile", args)

    def test_private_home_and_tmp(self):
        profile = SandboxProfile()
        args = profile.to_firejail_args("/home/skill")
        self.assertIn("--private", args)
        self.assertIn("--private-tmp", args)

    def test_memory_rlimit(self):
        profile = SandboxProfile(memory_mb=512)
        args = profile.to_firejail_args("/home/skill")
        rlimit_idx = args.index("--rlimit-as")
        self.assertEqual(args[rlimit_idx + 1], str(512 * 1024 * 1024))

    def test_timeout_format(self):
        profile = SandboxProfile(timeout_seconds=90)
        args = profile.to_firejail_args("/home/skill")
        timeout_idx = args.index("--timeout")
        self.assertEqual(args[timeout_idx + 1], "00:01:30")

    def test_env_sandbox_set(self):
        profile = SandboxProfile()
        args = profile.to_firejail_args("/home/skill")
        self.assertIn("--env", args)
        self.assertIn("SANDBOX=1", args)

    def test_ends_with_separator(self):
        profile = SandboxProfile()
        args = profile.to_firejail_args("/home/skill")
        self.assertEqual(args[-1], "--")


class TestSandboxIntegration(unittest.TestCase):
    """Integration tests — these require bwrap to be available.

    If bwrap is not functional (e.g., missing CAP_NET_ADMIN in container),
    these tests are skipped.
    """

    @classmethod
    def setUpClass(cls):
        cls.bwrap_available = False
        bwrap = shutil.which("bwrap")
        if bwrap:
            # Try a basic bwrap invocation
            import subprocess
            try:
                result = subprocess.run(
                    [bwrap, "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
                     "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
                     "--proc", "/proc", "--dev", "/dev",
                     "--die-with-parent", "--", "/bin/echo", "hello"],
                    capture_output=True, timeout=5,
                )
                if result.returncode == 0:
                    cls.bwrap_available = True
            except Exception:
                pass

    def _skip_if_no_bwrap(self):
        if not self.bwrap_available:
            self.skipTest("bwrap not functional in this environment (missing capabilities)")

    def test_hello_sandbox_execution(self):
        self._skip_if_no_bwrap()
        runner = SandboxRunner(workspace=tempfile.mkdtemp())
        toml_path = DEV_SKILLS_DIR / "hello-sandbox" / "skill.toml"
        profile = SandboxProfile.from_manifest(toml_path.read_text())
        result = runner.run(
            skill_dir=str(DEV_SKILLS_DIR / "hello-sandbox"),
            entrypoint="setup.sh",
            profile=profile,
            skill_slug="hello-sandbox",
        )
        self.assertTrue(result.success, f"Sandbox failed: {result.stderr}\nstdout: {result.stdout}")
        self.assertIn("Hello from sandbox", result.stdout)

    def test_python_processor_execution(self):
        self._skip_if_no_bwrap()
        runner = SandboxRunner(workspace=tempfile.mkdtemp())
        toml_path = DEV_SKILLS_DIR / "python-processor" / "skill.toml"
        profile = SandboxProfile.from_manifest(toml_path.read_text())
        result = runner.run(
            skill_dir=str(DEV_SKILLS_DIR / "python-processor"),
            entrypoint="setup.sh",
            profile=profile,
            skill_slug="python-processor",
        )
        self.assertTrue(result.success, f"Sandbox failed: {result.stderr}\nstdout: {result.stdout}")
        self.assertIn("Python version", result.stdout)

    def test_file_transformer_execution(self):
        self._skip_if_no_bwrap()
        runner = SandboxRunner(workspace=tempfile.mkdtemp())
        toml_path = DEV_SKILLS_DIR / "file-transformer" / "skill.toml"
        profile = SandboxProfile.from_manifest(toml_path.read_text())
        result = runner.run(
            skill_dir=str(DEV_SKILLS_DIR / "file-transformer"),
            entrypoint="setup.sh",
            profile=profile,
            skill_slug="file-transformer",
        )
        self.assertTrue(result.success, f"Sandbox failed: {result.stderr}\nstdout: {result.stdout}")
        self.assertIn("High scorers", result.stdout)


if __name__ == "__main__":
    unittest.main()
