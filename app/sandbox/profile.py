"""Sandbox profile — auto-generated from skill.toml [sandbox] declarations.

Parses the [sandbox] block from a skill manifest and produces command-line
arguments for the sandbox backend (firejail or bubblewrap).

[sandbox] block format (TOML):
    [sandbox]
    network_allow = ["api.github.com", "registry.npmjs.org"]
    fs_write = ["/tmp", "/home/skill/.cache"]
    exec_allow = ["python3", "node", "bash", "sh"]
    memory_mb = 512
    timeout_seconds = 120
    env_pass = ["HOME", "PATH", "LANG"]
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class SandboxProfile:
    """Immutable profile derived from a skill's [sandbox] manifest block."""

    # Network — domains allowed for outbound connections
    network_allow: list[str] = field(default_factory=list)

    # Filesystem — dirs the skill may write to (inside sandbox)
    fs_write: list[str] = field(default_factory=list)

    # Executables the skill is allowed to run
    exec_allow: list[str] = field(default_factory=list)

    # Resource limits
    memory_mb: int = 256
    timeout_seconds: int = 120

    # Environment variables to pass through into sandbox
    env_pass: list[str] = field(default_factory=lambda: ["PATH", "LANG", "HOME"])

    @classmethod
    def from_manifest(cls, skill_toml: str) -> SandboxProfile:
        """Parse a skill.toml string and extract the [sandbox] block."""
        try:
            data = tomllib.loads(skill_toml)
        # Rationale: TOML parse error for sandbox profile → re-raise as ValueError with context
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Invalid TOML in skill manifest: {exc}") from exc

        sandbox = data.get("sandbox", {})
        return cls(
            network_allow=sandbox.get("network_allow", []),
            fs_write=sandbox.get("fs_write", []),
            exec_allow=sandbox.get("exec_allow", []),
            memory_mb=sandbox.get("memory_mb", 256),
            timeout_seconds=sandbox.get("timeout_seconds", 120),
            env_pass=sandbox.get("env_pass", ["PATH", "LANG", "HOME"]),
        )

    @classmethod
    def default(cls) -> SandboxProfile:
        """Conservative default profile — no network, no writes, 256MB, 60s."""
        return cls(
            network_allow=[],
            fs_write=["/tmp"],
            exec_allow=["python3", "node", "bash", "sh"],
            memory_mb=256,
            timeout_seconds=60,
        )

    def validate(self) -> list[str]:
        """Return list of validation warnings (empty = valid).

        Raises ValueError if any network_allow entry is an IP literal that refers
        to a loopback, link-local, private, reserved, multicast, or unspecified
        address, or a hostname that violates RFC 1035 rules.
        """
        warnings: list[str] = []

        # RFC 1035 hostname label pattern: 1-63 chars, alphanumeric + hyphen,
        # no leading/trailing hyphen.
        _LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$|^[a-z0-9]$", re.IGNORECASE)

        # Check network domains look reasonable
        for host in self.network_allow:
            # Issue #9 fix: probe as IP literal first.
            try:
                ip = ipaddress.ip_address(host)
                if (
                    ip.is_loopback
                    or ip.is_link_local
                    or ip.is_private
                    or ip.is_reserved
                    or ip.is_multicast
                    or ip.is_unspecified
                    or not ip.is_global
                ):
                    raise ValueError(f"Disallowed IP literal in network_allow: {host!r}")
                # Public IP literal — allowed (but unusual), no warning needed.
            except ValueError as exc:
                # Re-raise if we produced it ourselves (disallowed IP).
                if "Disallowed IP literal" in str(exc):
                    raise
                # Not an IP literal — apply hostname rules.
                if host.lower() == "localhost":
                    raise ValueError(f"Disallowed hostname in network_allow: {host!r} (loopback alias)")
                # Validate RFC 1035 hostname structure.
                if len(host) > 253:
                    raise ValueError(f"network_allow hostname too long (>253 chars): {host!r}")
                labels = host.split(".")
                for label in labels:
                    if not label:
                        raise ValueError(f"network_allow hostname has empty label: {host!r}")
                    if not _LABEL_RE.match(label):
                        raise ValueError(
                            f"network_allow hostname label fails RFC 1035 rules: {label!r} in {host!r}"
                        )

        # Check fs_write paths are absolute and reasonable
        for path in self.fs_write:
            if not path.startswith("/"):
                warnings.append(f"fs_write path should be absolute: {path!r}")

        # Check exec_allow doesn't include dangerous binaries
        dangerous = {"rm", "mkfs", "dd", "fdisk", "format", "chmod", "chown", "sudo", "su"}
        for binary in self.exec_allow:
            basename = Path(binary).name
            if basename in dangerous:
                warnings.append(f"exec_allow includes dangerous binary: {binary!r}")

        # Reasonable resource limits
        if self.memory_mb > 4096:
            warnings.append(f"memory_mb={self.memory_mb} seems very high, cap at 4096")
        if self.timeout_seconds > 600:
            warnings.append(f"timeout_seconds={self.timeout_seconds} is very long, cap at 600")

        return warnings

    def to_firejail_args(self, skill_dir: str) -> list[str]:
        """Generate firejail command-line arguments from this profile.

        Args:
            skill_dir: Host path to the skill's checkout directory.
                       Must be under the user's home directory for
                       firejail access (--private doesn't hide it
                       because we use --private=skill_dir).

        Returns:
            List of firejail CLI arguments (excluding 'firejail' itself).
        """
        args: list[str] = []

        # ── Use a clean profile (no user profile overrides) ──
        args.append("--noprofile")

        # ── Filesystem isolation ──
        # --private=DIR creates an empty home and bind-mounts DIR as home.
        # This gives the sandbox an isolated home with only skill files.
        # The skill dir must be under the user's real home for firejail.
        args.extend(["--private=" + skill_dir])
        # Private /tmp to avoid writing to host tmp
        args.append("--private-tmp")

        # ── Network ──
        if not self.network_allow:
            args.append("--net=none")
        # When network_allow is non-empty, the runner starts a domain-filtering
        # CONNECT proxy and injects http_proxy/https_proxy env vars into the
        # sandbox. Firejail allows full network but the proxy blocks
        # non-allowlisted domains with 403 Forbidden.

        # ── Resource limits ──
        # Memory limit (RLIMIT_AS equivalent)
        mem_bytes = self.memory_mb * 1024 * 1024
        args.extend(["--rlimit-as=" + str(mem_bytes)])

        # Timeout (format: HH:MM:SS)
        mins = self.timeout_seconds // 60
        secs = self.timeout_seconds % 60
        timeout_str = f"00:{mins:02d}:{secs:02d}"
        args.extend(["--timeout=" + timeout_str])

        # ── Process restrictions ──
        # Note: --die-with-parent not available in all firejail versions; omitted.

        # ── Environment ──
        for env_var in self.env_pass:
            val = os.environ.get(env_var)
            if val is not None:
                args.append(f"--env={env_var}={val}")

        args.append("--env=SANDBOX=1")
        args.append(f"--env=SKILL_HOME={skill_dir}")

        # ── Shell command separator ──
        args.append("--")

        return args

    def to_bwrap_args(self, sandbox_root: str, workdir: str) -> list[str]:
        """Generate bubblewrap command-line arguments from this profile.

        NOTE: bwrap requires user namespace support (newuidmap) which may
        not be available on all hosts. Firejail is the preferred backend.

        Args:
            sandbox_root: Host directory to use as sandbox root filesystem.
            workdir: Working directory inside sandbox for skill execution.

        Returns:
            List of bwrap CLI arguments (excluding 'bwrap' itself).
        """
        args: list[str] = []

        # ── Root filesystem: read-only host dirs ──
        args.extend(["--ro-bind", "/usr", "/usr"])
        if os.path.exists("/lib"):
            args.extend(["--ro-bind", "/lib", "/lib"])
        if os.path.exists("/lib64"):
            args.extend(["--ro-bind", "/lib64", "/lib64"])
        if os.path.exists("/bin"):
            args.extend(["--ro-bind", "/bin", "/bin"])
        if os.path.exists("/sbin"):
            args.extend(["--ro-bind", "/sbin", "/sbin"])

        # Proc
        args.extend(["--proc", "/proc"])

        # Dev
        args.extend(["--dev", "/dev"])

        # Only bind resolv.conf if network is allowed
        if self.network_allow and os.path.exists("/etc/resolv.conf"):
            args.extend(["--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf"])

        # Sandbox root as /skill
        args.extend(["--bind", sandbox_root, "/skill"])
        args.extend(["--chdir", "/skill"])

        # ── Filesystem writes ──
        args.extend(["--bind", os.path.join(sandbox_root, "_tmp"), "/tmp"])
        for writable in self.fs_write:
            if writable == "/tmp":
                continue
            host_path = os.path.join(sandbox_root, "_writable", writable.strip("/"))
            args.extend(["--bind", host_path, writable])

        # ── Network ──
        if not self.network_allow:
            args.append("--unshare-net")

        # ── Resource limits ──
        args.append("--die-with-parent")

        # ── Environment ──
        for env_var in self.env_pass:
            val = os.environ.get(env_var)
            if val is not None:
                args.extend(["--setenv", env_var, val])

        args.extend(["--setenv", "SANDBOX", "1"])
        args.extend(["--setenv", "SKILL_HOME", "/skill"])

        return args
