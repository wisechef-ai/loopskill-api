"""Cron registrars (Phase F.4).

Each backend exposes:
    register(cron_spec, *, skill_slug) -> CronHandle
    unregister(handle)                 -> bool

The orchestrator picks the first available backend in this order on Linux:
hermes → systemd-timer; on macOS: hermes → launchd; on Windows: windows_task.
"""

from .base import CronHandle

__all__ = ["CronHandle"]
