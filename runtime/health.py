"""Aggregate health orchestrator (Phase F.5).

After services / binaries / crons have been provisioned, run a final
acceptance pass:
  * each declared service responds to its health URL within 30s
  * each declared binary responds to ``--version`` matching the declared minimum
  * each cron registered without conflict (caller passes its CronHandle list)

Failure → caller (the install orchestrator) triggers F.6 rollback.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from runtime.services.base import ServiceHandle
from runtime.services import docker_compose, systemd_user, launchd as svc_launchd
from runtime.cron.base import CronHandle


HEALTH_TIMEOUT_S = 30.0


@dataclass
class Check:
    name: str
    type: str  # "service" | "binary" | "cron"
    status: str  # "ok" | "fail" | "skip"
    ms: float = 0.0
    message: str = ""


@dataclass
class HealthReport:
    ok: bool
    checks: list[Check] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [asdict(c) for c in self.checks]}


_BACKEND_HEALTH = {
    "docker-compose": docker_compose.health,
    "systemd-user": systemd_user.health,
    "launchd": svc_launchd.health,
}


def check_services(handles: Iterable[ServiceHandle], *, deadline_s: float = HEALTH_TIMEOUT_S,
                   _http=None, _runner=subprocess.run, _now=time.monotonic,
                   _sleep=time.sleep) -> list[Check]:
    """Poll each service every 1s up to ``deadline_s``; first ok wins."""
    out: list[Check] = []
    for h in handles:
        fn = _BACKEND_HEALTH.get(h.backend)
        if fn is None:
            out.append(Check(name=h.name, type="service", status="fail",
                             message=f"no health probe for backend {h.backend}"))
            continue

        started = _now()
        last_msg = ""
        last_latency = 0.0
        while _now() - started < deadline_s:
            try:
                if h.backend == "docker-compose":
                    res = fn(h, _http=_http) if _http is not None else fn(h)
                else:
                    res = fn(h, _runner=_runner)
            except TypeError:
                res = fn(h)
            last_msg = res.message
            last_latency = res.latency_ms
            if res.ok:
                out.append(Check(name=h.name, type="service", status="ok",
                                 ms=res.latency_ms, message=res.message))
                break
            _sleep(1.0)
        else:
            out.append(Check(name=h.name, type="service", status="fail",
                             ms=last_latency,
                             message=last_msg or f"timeout after {deadline_s:.0f}s"))
    return out


_VER_RE = re.compile(r"(\d+(?:\.\d+){0,3})")
_MIN_RE = re.compile(r"(?P<name>[A-Za-z0-9_.-]+)?\s*>=\s*(?P<ver>\d+(?:\.\d+){0,3})")


def _parse_minimum(spec: str) -> tuple[str | None, tuple[int, ...]] | None:
    m = _MIN_RE.search(spec)
    if not m:
        return None
    name = m.group("name") or None
    ver = tuple(int(x) for x in m.group("ver").split("."))
    return name, ver


def check_binaries(specs: Iterable[dict[str, Any]],
                   _runner=subprocess.run) -> list[Check]:
    out: list[Check] = []
    for spec in specs:
        name = spec.get("name") or (spec.get("provides") or ["?"])[0]
        minimum = spec.get("minimum")
        started = time.monotonic()
        try:
            cp = _runner([name, "--version"], capture_output=True, text=True, check=False)
        except FileNotFoundError:
            out.append(Check(name=name, type="binary", status="fail",
                             ms=(time.monotonic() - started) * 1000,
                             message="binary not found"))
            continue
        ms = (time.monotonic() - started) * 1000
        version_text = cp.stdout or cp.stderr
        if not minimum:
            out.append(Check(name=name, type="binary", status="ok", ms=ms,
                             message=version_text.strip()[:120]))
            continue
        parsed = _parse_minimum(minimum)
        m = _VER_RE.search(version_text)
        if not m or not parsed:
            out.append(Check(name=name, type="binary", status="fail", ms=ms,
                             message=f"could not parse version from '{version_text[:80]}'"))
            continue
        actual = tuple(int(x) for x in m.group(1).split("."))
        _, want = parsed
        if actual >= want:
            out.append(Check(name=name, type="binary", status="ok", ms=ms,
                             message=f"version {'.'.join(map(str, actual))} >= {'.'.join(map(str, want))}"))
        else:
            out.append(Check(name=name, type="binary", status="fail", ms=ms,
                             message=f"version {'.'.join(map(str, actual))} < {'.'.join(map(str, want))}"))
    return out


def check_crons(handles: Iterable[CronHandle]) -> list[Check]:
    """Cron registration is synchronous — by the time we have a handle it's
    already enabled. We do a sanity check that every name is unique."""
    out: list[Check] = []
    seen: set[str] = set()
    for h in handles:
        key = f"{h.backend}:{h.name}"
        if key in seen:
            out.append(Check(name=h.name, type="cron", status="fail",
                             message=f"duplicate cron registration: {key}"))
        else:
            seen.add(key)
            out.append(Check(name=h.name, type="cron", status="ok",
                             message=f"registered via {h.backend}"))
    return out


def aggregate(service_handles: Iterable[ServiceHandle],
              binary_specs: Iterable[dict[str, Any]],
              cron_handles: Iterable[CronHandle], **kwargs) -> HealthReport:
    """Run all three categories of check and aggregate.

    ``ok`` is true only if every check is status='ok'.
    """
    checks: list[Check] = []
    checks.extend(check_services(service_handles, **kwargs))
    checks.extend(check_binaries(binary_specs, _runner=kwargs.get("_runner", subprocess.run)))
    checks.extend(check_crons(cron_handles))
    ok = all(c.status == "ok" for c in checks)
    return HealthReport(ok=ok, checks=checks)
