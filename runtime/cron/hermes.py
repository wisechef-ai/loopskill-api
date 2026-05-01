"""Hermes cron backend — preferred when ~/.hermes/scheduler/jobs.json exists.

Hermes is the host-side scheduler that ships with our agent fleets. When it
is present we register jobs through its JSON queue rather than touching
systemd or launchd; that keeps the scheduler unified across hosts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import CronHandle


def hermes_jobs_path() -> Path:
    return Path.home() / ".hermes" / "scheduler" / "jobs.json"


def is_available() -> bool:
    return hermes_jobs_path().exists()


def _load() -> list[dict[str, Any]]:
    p = hermes_jobs_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _save(jobs: list[dict[str, Any]]) -> None:
    p = hermes_jobs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(jobs, indent=2))


def register(cron_spec: dict[str, Any], *, skill_slug: str) -> CronHandle:
    job_id = f"{skill_slug}.{cron_spec['name']}"
    jobs = _load()
    jobs = [j for j in jobs if j.get("id") != job_id]
    jobs.append({
        "id": job_id,
        "schedule": cron_spec["schedule"],
        "cmd": cron_spec["cmd"],
        "skill": skill_slug,
    })
    _save(jobs)
    return CronHandle(name=cron_spec["name"], backend="hermes",
                      schedule=cron_spec["schedule"], cmd=cron_spec["cmd"],
                      extra={"job_id": job_id})


def unregister(handle: CronHandle) -> bool:
    job_id = handle.extra.get("job_id")
    if not job_id:
        return False
    jobs = _load()
    new = [j for j in jobs if j.get("id") != job_id]
    if len(new) == len(jobs):
        return False
    _save(new)
    return True
