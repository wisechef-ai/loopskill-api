"""systemd --user service backend (Linux daemons without root)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .base import HealthStatus, ServiceHandle
from runtime.adapters.base import skill_root


def _unit_dir() -> Path:
    p = Path.home() / ".config" / "systemd" / "user"
    p.mkdir(parents=True, exist_ok=True)
    return p


def provision(service_spec: dict[str, Any], *, skill_slug: str,
              _runner=subprocess.run) -> ServiceHandle:
    name = service_spec["name"]
    unit_text = service_spec.get("unit")
    if not unit_text:
        raise ValueError(f"service '{name}' missing systemd unit body")

    unit_file = _unit_dir() / f"recipes-{skill_slug}-{name}.service"
    unit_file.write_text(unit_text)

    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", unit_file.name]):
        cp = _runner(cmd, capture_output=True, text=True, check=False)
        if cp.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {cp.stderr or cp.stdout}")

    return ServiceHandle(
        name=name,
        backend="systemd-user",
        workdir=str(skill_root(skill_slug)),
        health_url=service_spec.get("health"),
        extra={"unit_file": str(unit_file)},
    )


def health(handle: ServiceHandle, _runner=subprocess.run) -> HealthStatus:
    unit = Path(handle.extra["unit_file"]).name
    cp = _runner(["systemctl", "--user", "is-active", unit],
                 capture_output=True, text=True, check=False)
    active = cp.stdout.strip() == "active"
    return HealthStatus(ok=active, name=handle.name, backend=handle.backend,
                        message=cp.stdout.strip() or cp.stderr.strip())


def teardown(handle: ServiceHandle, _runner=subprocess.run) -> bool:
    unit_path = Path(handle.extra["unit_file"])
    name = unit_path.name
    _runner(["systemctl", "--user", "disable", "--now", name],
            capture_output=True, text=True, check=False)
    unit_path.unlink(missing_ok=True)
    cp = _runner(["systemctl", "--user", "daemon-reload"],
                 capture_output=True, text=True, check=False)
    return cp.returncode == 0
