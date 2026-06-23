"""Service-provisioner primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ServiceHandle:
    name: str
    backend: str  # "docker-compose" | "systemd-user" | "launchd"
    workdir: str | None = None
    health_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthStatus:
    ok: bool
    name: str
    backend: str
    latency_ms: float = 0.0
    message: str = ""
