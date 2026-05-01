"""systemd --user timer cron backend (Linux fallback)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .base import CronHandle


def _unit_dir() -> Path:
    p = Path.home() / ".config" / "systemd" / "user"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cron_to_oncalendar(cron: str) -> str:
    """Translate the small subset of cron we expect into systemd OnCalendar.

    Most of our recipe.yaml crons are simple ``H M * * *`` daily schedules.
    For anything more exotic, the recipe author should drop a hermes job.
    """
    parts = cron.split()
    if len(parts) != 5:
        return f"*-*-* {cron.replace(' ', ':')}:00"  # best effort
    minute, hour, dom, month, dow = parts
    if dom == "*" and month == "*" and dow == "*":
        return f"*-*-* {hour}:{minute}:00"
    return f"*-*-* {hour}:{minute}:00"


def register(cron_spec: dict[str, Any], *, skill_slug: str,
             _runner=subprocess.run) -> CronHandle:
    name = cron_spec["name"]
    base = f"recipes-{skill_slug}-{name}"
    service_unit = _unit_dir() / f"{base}.service"
    timer_unit = _unit_dir() / f"{base}.timer"

    service_unit.write_text(
        "[Unit]\n"
        f"Description=Recipes cron job: {skill_slug}/{name}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart=/bin/sh -c {_shellquote(cron_spec['cmd'])}\n"
    )
    timer_unit.write_text(
        "[Unit]\n"
        f"Description=Recipes timer: {skill_slug}/{name}\n\n"
        "[Timer]\n"
        f"OnCalendar={_cron_to_oncalendar(cron_spec['schedule'])}\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", timer_unit.name]):
        cp = _runner(cmd, capture_output=True, text=True, check=False)
        if cp.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {cp.stderr or cp.stdout}")

    return CronHandle(name=name, backend="systemd-timer",
                      schedule=cron_spec["schedule"], cmd=cron_spec["cmd"],
                      extra={"service": str(service_unit), "timer": str(timer_unit)})


def unregister(handle: CronHandle, _runner=subprocess.run) -> bool:
    timer = Path(handle.extra["timer"])
    service = Path(handle.extra["service"])
    _runner(["systemctl", "--user", "disable", "--now", timer.name],
            capture_output=True, text=True, check=False)
    timer.unlink(missing_ok=True)
    service.unlink(missing_ok=True)
    cp = _runner(["systemctl", "--user", "daemon-reload"],
                 capture_output=True, text=True, check=False)
    return cp.returncode == 0


def _shellquote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
