"""Sandbox runner — executes skill scripts inside a firejail (or bwrap) sandbox.

Uses firejail as the primary backend (SUID binary, works without user namespaces).
Falls back to bubblewrap if firejail is unavailable.

When a skill declares network_allow domains, a local domain-filtering proxy
is started and injected into the sandbox via http_proxy/https_proxy env vars.
This ensures that even when the sandbox has network access, only allowlisted
domains are reachable.

Usage:
    runner = SandboxRunner(workspace="/var/lib/wiserecipes/sandboxes")
    result = runner.run(
        skill_dir="/path/to/skill/checkout",
        entrypoint="setup.sh",
        profile=SandboxProfile.from_manifest(skill_toml),
    )
    print(result.exit_code, result.stdout, result.stderr)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import select
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.sandbox.profile import SandboxProfile

logger = logging.getLogger(__name__)

# Directory where sandbox workspaces live
DEFAULT_WORKSPACE = "/var/lib/wiserecipes/sandboxes"

# Maximum output we capture (bytes)
MAX_OUTPUT_BYTES = 1 * 1024 * 1024  # 1 MB


class SandboxError(RuntimeError):
    """Raised when the sandbox runner encounters an unrecoverable error."""
    pass


@dataclass
class SandboxResult:
    """Outcome of a sandboxed skill execution."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    sandbox_id: str
    memory_used_mb: Optional[float] = None
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None

    def to_dict(self) -> dict:
        return {
            "sandbox_id": self.sandbox_id,
            "exit_code": self.exit_code,
            "stdout": self.stdout[:5000],
            "stderr": self.stderr[:5000],
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration_seconds, 3),
            "memory_used_mb": self.memory_used_mb,
            "success": self.success,
            "error": self.error,
        }


class SandboxRunner:
    """Execute skill scripts inside firejail/bwrap sandboxes."""

    # Staging dir under $HOME so firejail can access it
    STAGING_BASE = os.path.expanduser("~/.wiserecipes-sandbox")

    def __init__(self, workspace: str = DEFAULT_WORKSPACE):
        self.workspace = workspace
        os.makedirs(workspace, exist_ok=True)
        os.makedirs(self.STAGING_BASE, exist_ok=True)
        self._backend = self._detect_backend()

    @staticmethod
    def _detect_backend() -> str:
        """Detect available sandbox backend: 'firejail' or 'bwrap'."""
        if shutil.which("firejail"):
            return "firejail"
        if shutil.which("bwrap"):
            return "bwrap"
        return "none"

    @property
    def backend(self) -> str:
        return self._backend

    def run(
        self,
        skill_dir: str,
        entrypoint: str,
        profile: SandboxProfile,
        skill_slug: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> SandboxResult:
        """Run a skill script inside a sandbox.

        Args:
            skill_dir: Host path to the skill's checkout directory.
            entrypoint: Script filename inside skill_dir to execute (e.g. "setup.sh").
            profile: Sandbox profile from skill.toml [sandbox] block.
            skill_slug: Optional slug for logging/telemetry.
            env: Additional env vars to pass into sandbox.

        Returns:
            SandboxResult with exit code, captured output, and timing.
        """
        sandbox_id = str(uuid.uuid4())[:12]
        start_time = time.monotonic()

        # Validate sandbox backend
        if self._backend == "none":
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                duration_seconds=0,
                sandbox_id=sandbox_id,
                error="No sandbox backend available (need firejail or bwrap)",
            )

        # Validate skill directory exists
        if not os.path.isdir(skill_dir):
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                duration_seconds=0,
                sandbox_id=sandbox_id,
                error=f"Skill directory does not exist: {skill_dir}",
            )

        # Validate entrypoint exists
        entrypoint_path = os.path.join(skill_dir, entrypoint)
        if not os.path.isfile(entrypoint_path):
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                duration_seconds=0,
                sandbox_id=sandbox_id,
                error=f"Entrypoint not found: {entrypoint}",
            )

        logger.info(
            f"Sandbox {sandbox_id}: running {entrypoint} for skill {skill_slug or 'unknown'} "
            f"(backend={self._backend}, mem={profile.memory_mb}MB, "
            f"timeout={profile.timeout_seconds}s, "
            f"net={'filtered:' + ','.join(profile.network_allow) if profile.network_allow else 'isolated'})"
        )

        try:
            if self._backend == "firejail":
                return self._run_firejail(
                    skill_dir, entrypoint, profile, sandbox_id, env, start_time
                )
            else:
                return self._run_bwrap(
                    skill_dir, entrypoint, profile, sandbox_id, env, start_time
                )
        except Exception as exc:
            duration = time.monotonic() - start_time
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                timed_out=False,
                duration_seconds=duration,
                sandbox_id=sandbox_id,
                error=f"Execution failed: {exc}",
            )

    def _run_firejail(
        self,
        skill_dir: str,
        entrypoint: str,
        profile: SandboxProfile,
        sandbox_id: str,
        env: Optional[dict[str, str]],
        start_time: float,
    ) -> SandboxResult:
        """Execute using firejail backend.

        Stages skill dir under $HOME/.wiserecipes-sandbox/{id}/ because
        firejail's default mount namespace only allows access to paths
        under the user's home directory.

        When network_allow is non-empty, starts a domain-filtering proxy
        on a local port and injects http_proxy/https_proxy env vars.
        """
        proxy = None
        proxy_port = None

        # Start domain proxy if network is allowed
        if profile.network_allow:
            try:
                proxy = self._start_domain_proxy_sync(profile.network_allow)
                proxy_port = proxy["port"]
                logger.info(f"Sandbox {sandbox_id}: domain proxy on port {proxy_port} allowing {profile.network_allow}")
            except Exception as exc:
                # Issue #8 fix: fail CLOSED — never run with unrestricted network.
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Network proxy could not start: {exc!r}. Refusing to run with unrestricted network.",
                    timed_out=False,
                    duration_seconds=time.monotonic() - start_time,
                    sandbox_id=sandbox_id,
                    error="proxy_failed",
                )

        try:
            # Stage skill dir under HOME for firejail access
            staged_dir = os.path.join(self.STAGING_BASE, sandbox_id)
            try:
                shutil.copytree(skill_dir, staged_dir)
                # Ensure entrypoint is executable
                os.chmod(os.path.join(staged_dir, entrypoint), 0o755)
            except Exception as exc:
                return SandboxResult(
                    exit_code=-1, stdout="", stderr=str(exc),
                    timed_out=False, duration_seconds=time.monotonic() - start_time,
                    sandbox_id=sandbox_id, error=f"Staging failed: {exc}",
                )

            # Build firejail args
            fj_args = profile.to_firejail_args(staged_dir)

            # Additional env vars
            merged_env = dict(env) if env else {}

            # Inject proxy env vars if proxy is running
            if proxy_port:
                merged_env["http_proxy"] = f"http://127.0.0.1:{proxy_port}"
                merged_env["https_proxy"] = f"http://127.0.0.1:{proxy_port}"
                merged_env["HTTP_PROXY"] = f"http://127.0.0.1:{proxy_port}"
                merged_env["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy_port}"
                merged_env["no_proxy"] = "localhost,127.0.0.1"
                merged_env["NO_PROXY"] = "localhost,127.0.0.1"

            for k, v in merged_env.items():
                fj_args.extend(["--env", f"{k}={v}"])

            # Build full command
            cmd = ["firejail"] + fj_args + ["./" + entrypoint]

            logger.debug(f"Sandbox {sandbox_id} firejail cmd: {' '.join(cmd)}")

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=profile.timeout_seconds + 10,
                    cwd=staged_dir,
                )
                duration = time.monotonic() - start_time

                stdout = self._parse_firejail_output(proc.stdout)
                stderr = self._parse_firejail_output(proc.stderr)

                # Detect firejail timeout
                timed_out = (
                    proc.returncode != 0
                    and "Parent is shutting down" in (proc.stderr.decode("utf-8", errors="replace"))
                    and proc.returncode == 1
                )

                return SandboxResult(
                    exit_code=proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=timed_out,
                    duration_seconds=duration,
                    sandbox_id=sandbox_id,
                )

            except subprocess.TimeoutExpired as exc:
                duration = time.monotonic() - start_time
                stdout = self._parse_firejail_output(exc.stdout or b"")
                stderr = self._parse_firejail_output(exc.stderr or b"")
                return SandboxResult(
                    exit_code=-1,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=True,
                    duration_seconds=duration,
                    sandbox_id=sandbox_id,
                    error=f"Execution timed out after {profile.timeout_seconds}s",
                )
            finally:
                self._cleanup(staged_dir)

        finally:
            # Stop domain proxy
            if proxy:
                self._stop_domain_proxy_sync(proxy)

    def _run_bwrap(
        self,
        skill_dir: str,
        entrypoint: str,
        profile: SandboxProfile,
        sandbox_id: str,
        env: Optional[dict[str, str]],
        start_time: float,
    ) -> SandboxResult:
        """Execute using bubblewrap backend (fallback).

        Same domain proxy integration as firejail path when network_allow is non-empty.
        """
        proxy = None
        proxy_port = None

        if profile.network_allow:
            try:
                proxy = self._start_domain_proxy_sync(profile.network_allow)
                proxy_port = proxy["port"]
            except Exception as exc:
                # Issue #8 fix: fail CLOSED — never run with unrestricted network.
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Network proxy could not start: {exc!r}. Refusing to run with unrestricted network.",
                    timed_out=False,
                    duration_seconds=time.monotonic() - start_time,
                    sandbox_id=sandbox_id,
                    error="proxy_failed",
                )

        try:
            sandbox_root = os.path.join(self.workspace, sandbox_id)

            try:
                os.makedirs(sandbox_root, exist_ok=True)
                os.makedirs(os.path.join(sandbox_root, "_tmp"), exist_ok=True)
                os.makedirs(os.path.join(sandbox_root, "_writable"), exist_ok=True)

                self._prepare_bwrap_root(skill_dir, sandbox_root, profile)

                bwrap_args = profile.to_bwrap_args(sandbox_root, "/skill")

                merged_env = dict(env) if env else {}
                if proxy_port:
                    merged_env["http_proxy"] = f"http://127.0.0.1:{proxy_port}"
                    merged_env["https_proxy"] = f"http://127.0.0.1:{proxy_port}"
                    merged_env["HTTP_PROXY"] = f"http://127.0.0.1:{proxy_port}"
                    merged_env["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy_port}"
                    merged_env["no_proxy"] = "localhost,127.0.0.1"
                    merged_env["NO_PROXY"] = "localhost,127.0.0.1"

                for k, v in merged_env.items():
                    bwrap_args.extend(["--setenv", k, v])

                cmd = ["bwrap"] + bwrap_args + ["--", "/bin/bash", "-c", f"cd /skill && ./{entrypoint}"]

                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=profile.timeout_seconds,
                )
                duration = time.monotonic() - start_time

                stdout = proc.stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
                stderr = proc.stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")

                return SandboxResult(
                    exit_code=proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=False,
                    duration_seconds=duration,
                    sandbox_id=sandbox_id,
                )

            except subprocess.TimeoutExpired as exc:
                duration = time.monotonic() - start_time
                stdout = (exc.stdout or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
                stderr = (exc.stderr or b"")[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
                return SandboxResult(
                    exit_code=-1,
                    stdout=stdout,
                    stderr=stderr,
                    timed_out=True,
                    duration_seconds=duration,
                    sandbox_id=sandbox_id,
                    error=f"Execution timed out after {profile.timeout_seconds}s",
                )
            finally:
                self._cleanup(sandbox_root)

        finally:
            if proxy:
                self._stop_domain_proxy_sync(proxy)

    @staticmethod
    def _start_domain_proxy_sync(allowed_domains: list[str]) -> dict:
        """Start domain proxy in a background process. Returns proxy info dict."""
        # Run the domain proxy as a subprocess so it can be cleanly killed
        import sys

        proxy_script = os.path.join(os.path.dirname(__file__), "_run_proxy.py")
        if not os.path.exists(proxy_script):
            raise RuntimeError(f"Proxy script not found: {proxy_script}")

        proc = subprocess.Popen(
            [sys.executable, proxy_script] + allowed_domains,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # F-API-04: select-based read with 5s timeout (replaces blocking readline)
        deadline = time.monotonic() + 5.0
        port_line = None
        while time.monotonic() < deadline:
            rl, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.5)
            if proc.stdout in rl:
                raw = proc.stdout.readline()
                port_line = raw.decode().rstrip("\n")
                if port_line:
                    break
            if proc.stderr in rl:
                _err = proc.stderr.readline()  # consume but keep reading stdout

        if not port_line:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            remaining_stderr = b""
            try:
                remaining_stderr = proc.stderr.read()
            except Exception:
                pass
            raise SandboxError(
                f"proxy did not emit port within 5s; stderr={remaining_stderr!r}"
            )

        try:
            port = int(port_line.strip())
        except ValueError as exc:
            proc.kill()
            raise SandboxError(f"Failed to start domain proxy: bad port {port_line!r}") from exc

        return {"process": proc, "port": port}

    @staticmethod
    def _stop_domain_proxy_sync(proxy: dict):
        """Stop the domain proxy process."""
        proc = proxy.get("process")
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    @staticmethod
    def _parse_firejail_output(raw: bytes) -> str:
        """Parse firejail output, stripping its status messages."""
        text = raw.decode("utf-8", errors="replace")
        lines = text.split("\n")
        filtered = []
        for line in lines:
            if line.startswith("Parent pid"):
                continue
            if line.startswith("Child process initialized"):
                continue
            if line.startswith("Parent is shutting down"):
                continue
            filtered.append(line)
        result = "\n".join(filtered).strip()
        return result[:MAX_OUTPUT_BYTES]

    def _prepare_bwrap_root(
        self, skill_dir: str, sandbox_root: str, profile: SandboxProfile
    ) -> None:
        """Copy skill files into sandbox root and create writable dirs (for bwrap)."""
        for item in os.listdir(skill_dir):
            src = os.path.join(skill_dir, item)
            dst = os.path.join(sandbox_root, item)
            if item.startswith("_"):
                continue
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        for f in os.listdir(sandbox_root):
            fp = os.path.join(sandbox_root, f)
            if os.path.isfile(fp) and (f.endswith(".sh") or not os.path.splitext(f)[1]):
                os.chmod(fp, 0o755)

        for writable_path in profile.fs_write:
            if writable_path == "/tmp":
                continue
            host_path = os.path.join(sandbox_root, "_writable", writable_path.strip("/"))
            os.makedirs(host_path, exist_ok=True)

    def _cleanup(self, sandbox_root: str) -> None:
        """Remove sandbox workspace after execution."""
        try:
            if os.path.exists(sandbox_root):
                shutil.rmtree(sandbox_root, ignore_errors=True)
        except Exception as exc:
            logger.warning(f"Failed to cleanup sandbox {sandbox_root}: {exc}")
