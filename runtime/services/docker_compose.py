"""docker-compose service backend.

We don't actually run docker-compose in this module's tests; we shell out via
``_runner`` which is monkeypatched by tests. Production wires the real
``subprocess.run`` through.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from .base import HealthStatus, ServiceHandle
from runtime.adapters.base import skill_root


def _compose_cmd() -> list[str]:
    """Prefer ``docker compose`` (v2 plugin) over the legacy ``docker-compose`` binary."""
    return ["docker", "compose"]


def provision(service_spec: dict[str, Any], *, skill_slug: str,
              _runner=subprocess.run) -> ServiceHandle:
    """Bring a compose stack up and return a handle for later health/teardown."""
    workdir = skill_root(skill_slug)
    compose_path = service_spec.get("compose")
    if not compose_path:
        raise ValueError(f"service '{service_spec.get('name')}' missing compose path")

    cp = _runner([*_compose_cmd(), "-f", compose_path, "up", "-d"],
                 cwd=str(workdir), capture_output=True, text=True, check=False)
    if cp.returncode != 0:
        raise RuntimeError(f"docker compose up failed: {cp.stderr or cp.stdout}")

    return ServiceHandle(
        name=service_spec["name"],
        backend="docker-compose",
        workdir=str(workdir),
        health_url=service_spec.get("health"),
        extra={"compose": compose_path, "port": service_spec.get("port")},
    )


def up(handle: ServiceHandle, _runner=subprocess.run) -> bool:
    cp = _runner([*_compose_cmd(), "-f", handle.extra["compose"], "up", "-d"],
                 cwd=handle.workdir, capture_output=True, text=True, check=False)
    return cp.returncode == 0


def down(handle: ServiceHandle, _runner=subprocess.run) -> bool:
    cp = _runner([*_compose_cmd(), "-f", handle.extra["compose"], "down"],
                 cwd=handle.workdir, capture_output=True, text=True, check=False)
    return cp.returncode == 0


def health(handle: ServiceHandle, _http=None) -> HealthStatus:
    if not handle.health_url:
        return HealthStatus(ok=True, name=handle.name, backend=handle.backend,
                            message="no health URL declared; assumed ok")
    url = handle.health_url
    if url.upper().startswith("GET "):
        url = url[4:].strip()
    if _http is None:
        import httpx
        _http = httpx
    started = time.monotonic()
    try:
        r = _http.get(url, timeout=5.0)
        ms = (time.monotonic() - started) * 1000
        ok = 200 <= getattr(r, "status_code", 0) < 300
        return HealthStatus(ok=ok, name=handle.name, backend=handle.backend,
                            latency_ms=ms,
                            message=f"http {getattr(r, 'status_code', '?')}")
    except Exception as exc:
        ms = (time.monotonic() - started) * 1000
        return HealthStatus(ok=False, name=handle.name, backend=handle.backend,
                            latency_ms=ms, message=str(exc))


def teardown(handle: ServiceHandle, _runner=subprocess.run) -> bool:
    return down(handle, _runner=_runner)
