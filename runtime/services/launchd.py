"""launchd service backend (macOS daemons under the user agent domain)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .base import HealthStatus, ServiceHandle
from runtime.adapters.base import skill_root


def _agents_dir() -> Path:
    p = Path.home() / "Library" / "LaunchAgents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def provision(service_spec: dict[str, Any], *, skill_slug: str,
              _runner=subprocess.run) -> ServiceHandle:
    name = service_spec["name"]
    plist = service_spec.get("plist")
    if not plist:
        raise ValueError(f"service '{name}' missing plist body")

    label = f"ai.wisechef.recipes.{skill_slug}.{name}"
    plist_path = _agents_dir() / f"{label}.plist"
    plist_path.write_text(plist)

    cp = _runner(["launchctl", "load", "-w", str(plist_path)],
                 capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        raise RuntimeError(f"launchctl load failed: {cp.stderr or cp.stdout}")

    return ServiceHandle(
        name=name,
        backend="launchd",
        workdir=str(skill_root(skill_slug)),
        health_url=service_spec.get("health"),
        extra={"plist": str(plist_path), "label": label},
    )


def health(handle: ServiceHandle, _runner=subprocess.run) -> HealthStatus:
    cp = _runner(["launchctl", "list", handle.extra["label"]],
                 capture_output=True, text=True, check=False)
    ok = cp.returncode == 0
    return HealthStatus(ok=ok, name=handle.name, backend=handle.backend,
                        message=cp.stdout.strip() or cp.stderr.strip())


def teardown(handle: ServiceHandle, _runner=subprocess.run) -> bool:
    plist_path = Path(handle.extra["plist"])
    _runner(["launchctl", "unload", "-w", str(plist_path)],
            capture_output=True, text=True, check=False)
    plist_path.unlink(missing_ok=True)
    return True
