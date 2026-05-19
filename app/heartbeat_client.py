"""Heartbeat client — opt-OUT via RECIPES_TELEMETRY env.

This module ships in the API repo so the schema lives next to the server.
Installer (recipes-skill / recipes-installer) imports it. The default
posture is *opt-out* — to disable, set `RECIPES_TELEMETRY=off` (also
accepted: `0`, `false`, case-insensitive).

The transport accepts any object with a `.request(method, url, body=, headers=)`
signature so we don't pin a network library at import-time, and so tests can
substitute a mock to assert *zero* outbound traffic in opt-out mode.
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any

_DISABLED_VALUES = frozenset({"off", "0", "false", "no", "disable", "disabled"})


def telemetry_disabled() -> bool:
    """Return True if the RECIPES_TELEMETRY env var is set to a disabled value."""
    raw = os.environ.get("RECIPES_TELEMETRY")
    if raw is None:
        return False
    return raw.strip().lower() in _DISABLED_VALUES


def send_heartbeat(
    *,
    endpoint: str,
    pool: Any | None = None,
    salt: str | None = None,
    last_seen_day: date | None = None,
) -> dict:
    """Post a heartbeat. Returns {"skipped": True} when opt-out is set,
    otherwise {"sent": True, "status": <int>}.
    """
    if telemetry_disabled():
        return {"skipped": True, "reason": "RECIPES_TELEMETRY=off"}

    if salt is None:
        # Generate a fresh salt per call — clients are expected to persist
        # one and pass it in. We never read or write a salt file ourselves.
        import secrets

        salt = secrets.token_hex(16)
    if last_seen_day is None:
        last_seen_day = date.today()

    body = json.dumps({"salt": salt, "last_seen_day": last_seen_day.isoformat()}).encode("utf-8")
    headers = {"Content-Type": "application/json"}

    if pool is None:
        import urllib3

        pool = urllib3.PoolManager()

    resp = pool.request("POST", endpoint, body=body, headers=headers)
    return {"sent": True, "status": getattr(resp, "status", None)}
