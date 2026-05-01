"""Cron handle dataclass."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CronHandle:
    name: str
    backend: str  # "hermes" | "systemd-timer" | "launchd" | "windows-task"
    schedule: str
    cmd: str
    extra: dict[str, Any] = field(default_factory=dict)
