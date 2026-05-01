"""Windows Task Scheduler cron backend (Studio tier)."""

from __future__ import annotations

import subprocess
from typing import Any

from .base import CronHandle


def register(cron_spec: dict[str, Any], *, skill_slug: str,
             _runner=subprocess.run) -> CronHandle:
    name = f"recipes-{skill_slug}-{cron_spec['name']}"
    cp = _runner(
        ["schtasks", "/Create", "/F", "/SC", "DAILY", "/TN", name,
         "/TR", cron_spec["cmd"], "/ST", _start_time(cron_spec["schedule"])],
        capture_output=True, text=True, check=False,
    )
    if cp.returncode != 0:
        raise RuntimeError(f"schtasks /Create failed: {cp.stderr or cp.stdout}")
    return CronHandle(name=cron_spec["name"], backend="windows-task",
                      schedule=cron_spec["schedule"], cmd=cron_spec["cmd"],
                      extra={"task_name": name})


def unregister(handle: CronHandle, _runner=subprocess.run) -> bool:
    cp = _runner(["schtasks", "/Delete", "/F", "/TN", handle.extra["task_name"]],
                 capture_output=True, text=True, check=False)
    return cp.returncode == 0


def _start_time(cron: str) -> str:
    parts = cron.split()
    if len(parts) != 5:
        return "03:00"
    minute, hour, *_ = parts
    try:
        return f"{int(hour):02d}:{int(minute):02d}"
    except ValueError:
        return "03:00"
