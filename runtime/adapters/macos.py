"""macOS adapter — brew → curl-sha256 fallback."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .base import AdapterPlan, InstallResult, skill_root, which
from . import linux as _linux  # share curl helper


def resolve(binary_spec: dict[str, Any]) -> AdapterPlan:
    name = binary_spec.get("name") or (binary_spec.get("provides") or ["?"])[0]
    package = binary_spec.get("package") or name
    if which("brew"):
        return AdapterPlan(name=name, method="brew", package=package)
    url = binary_spec.get("url") or binary_spec.get("release_source")
    return AdapterPlan(
        name=name,
        method="curl",
        url=url,
        sha256=binary_spec.get("sha256"),
        target_path=skill_root(binary_spec.get("_skill_slug", "_unscoped")) / "bin" / name,
    )


def install(plan: AdapterPlan, _runner=subprocess.run, _http=None) -> InstallResult:
    if plan.method == "brew":
        cp = _runner(["brew", "install", plan.package or plan.name],
                     capture_output=True, text=True, check=False)
        return InstallResult(ok=cp.returncode == 0, name=plan.name, method="brew",
                             message=cp.stderr or cp.stdout)
    if plan.method == "curl":
        return _linux._curl_sha256(plan, _http=_http)
    return InstallResult(ok=False, name=plan.name, method=plan.method,
                         message=f"unknown method '{plan.method}'")


def uninstall(name: str, _runner=subprocess.run) -> bool:
    removed = False
    if which("brew"):
        cp = _runner(["brew", "uninstall", name], capture_output=True, text=True, check=False)
        if cp.returncode == 0:
            removed = True

    root = skill_root("_unscoped").parent
    if root.exists():
        for skill_dir in root.iterdir():
            manifest = skill_dir / "installed.json"
            if not manifest.exists():
                continue
            try:
                entries = json.loads(manifest.read_text())
            except json.JSONDecodeError:
                continue
            kept = []
            for e in entries:
                if e.get("name") == name and e.get("method") == "curl":
                    if e.get("path"):
                        Path(e["path"]).unlink(missing_ok=True)
                    removed = True
                else:
                    kept.append(e)
            manifest.write_text(json.dumps(kept, indent=2))

    return removed
