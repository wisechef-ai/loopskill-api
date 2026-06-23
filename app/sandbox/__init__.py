"""Sandbox runner for WiseRecipes skill execution.

Uses bubblewrap (bwrap) or firejail to isolate skill setup/execution with:
  - Network egress filtering per manifest allowlist (via domain proxy)
  - Filesystem write restrictions (read-only root + whitelisted dirs)
  - Resource limits (memory, CPU, time)
  - Domain-level network filtering via local CONNECT proxy

Triggered when a skill's skill.toml declares a [sandbox] block.
"""

from app.sandbox.domain_proxy import DomainProxy
from app.sandbox.profile import SandboxProfile
from app.sandbox.runner import SandboxResult, SandboxRunner

__all__ = ["SandboxRunner", "SandboxResult", "SandboxProfile", "DomainProxy"]
