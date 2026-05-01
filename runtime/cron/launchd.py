"""launchd cron backend (macOS fallback)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .base import CronHandle


def _agents_dir() -> Path:
    p = Path.home() / "Library" / "LaunchAgents"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _calendar_interval(cron: str) -> str:
    """Render a CalendarInterval block. Falls back to daily 03:00 if cron is exotic."""
    parts = cron.split()
    if len(parts) != 5:
        return "<key>Hour</key><integer>3</integer><key>Minute</key><integer>0</integer>"
    minute, hour, *_ = parts
    try:
        h = int(hour)
        m = int(minute)
    except ValueError:
        h, m = 3, 0
    return f"<key>Hour</key><integer>{h}</integer><key>Minute</key><integer>{m}</integer>"


def register(cron_spec: dict[str, Any], *, skill_slug: str,
             _runner=subprocess.run) -> CronHandle:
    name = cron_spec["name"]
    label = f"ai.wisechef.recipes.{skill_slug}.{name}"
    plist_path = _agents_dir() / f"{label}.plist"

    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        f'  <key>Label</key><string>{label}</string>\n'
        '  <key>ProgramArguments</key>\n'
        '  <array>\n'
        '    <string>/bin/sh</string><string>-c</string>'
        f'<string>{_xmlescape(cron_spec["cmd"])}</string>\n'
        '  </array>\n'
        '  <key>StartCalendarInterval</key>\n'
        f'  <dict>{_calendar_interval(cron_spec["schedule"])}</dict>\n'
        '</dict>\n</plist>\n'
    )
    plist_path.write_text(plist)
    cp = _runner(["launchctl", "load", "-w", str(plist_path)],
                 capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        raise RuntimeError(f"launchctl load failed: {cp.stderr or cp.stdout}")
    return CronHandle(name=name, backend="launchd", schedule=cron_spec["schedule"],
                      cmd=cron_spec["cmd"], extra={"plist": str(plist_path), "label": label})


def unregister(handle: CronHandle, _runner=subprocess.run) -> bool:
    plist = Path(handle.extra["plist"])
    _runner(["launchctl", "unload", "-w", str(plist)],
            capture_output=True, text=True, check=False)
    plist.unlink(missing_ok=True)
    return True


def _xmlescape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
              .replace('"', "&quot;").replace("'", "&apos;"))
