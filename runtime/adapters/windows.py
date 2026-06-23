"""Windows adapter — winget only (Studio tier, minimal day-1 coverage)."""

from __future__ import annotations

import subprocess
from typing import Any

from .base import AdapterPlan, InstallResult, which


def resolve(binary_spec: dict[str, Any]) -> AdapterPlan:
    name = binary_spec.get("name") or (binary_spec.get("provides") or ["?"])[0]
    package = binary_spec.get("package") or name
    return AdapterPlan(name=name, method="winget", package=package)


def install(plan: AdapterPlan, _runner=subprocess.run) -> InstallResult:
    if not which("winget"):
        return InstallResult(ok=False, name=plan.name, method="winget",
                             message="winget not available on this host")
    cp = _runner(
        ["winget", "install", "--silent", "--accept-source-agreements",
         "--accept-package-agreements", "-e", "--id", plan.package or plan.name],
        capture_output=True, text=True, check=False,
    )
    return InstallResult(ok=cp.returncode == 0, name=plan.name, method="winget",
                         message=cp.stderr or cp.stdout)


def uninstall(name: str, _runner=subprocess.run) -> bool:
    if not which("winget"):
        return False
    cp = _runner(["winget", "uninstall", "--silent", "-e", "--id", name],
                 capture_output=True, text=True, check=False)
    return cp.returncode == 0
