"""F.5 — health orchestrator aggregates correctly + 30s timeout fail."""

from __future__ import annotations

import subprocess

import pytest

from runtime import health
from runtime.cron.base import CronHandle
from runtime.services.base import HealthStatus, ServiceHandle


def _ok_runner_for_version(version_text):
    def fake(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=version_text, stderr="")
    return fake


def test_check_binaries_minimum_satisfied():
    specs = [{"name": "rg", "minimum": "rg>=13.0"}]
    runner = _ok_runner_for_version("ripgrep 14.0.3 (rev abc)\n")
    out = health.check_binaries(specs, _runner=runner)
    assert out[0].status == "ok"


def test_check_binaries_minimum_violated():
    specs = [{"name": "rg", "minimum": "rg>=15"}]
    runner = _ok_runner_for_version("ripgrep 14.0.3 (rev abc)\n")
    out = health.check_binaries(specs, _runner=runner)
    assert out[0].status == "fail"
    assert "<" in out[0].message


def test_check_binaries_missing_binary():
    def runner(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    out = health.check_binaries([{"name": "nope"}], _runner=runner)
    assert out[0].status == "fail"
    assert "not found" in out[0].message


def test_check_services_succeeds_first_try(monkeypatch):
    handle = ServiceHandle(name="api", backend="docker-compose",
                           workdir="/x", health_url="GET http://localhost:1/h",
                           extra={"compose": "x.yml"})

    def fake_health(h, _http=None):
        return HealthStatus(ok=True, name=h.name, backend=h.backend, latency_ms=5.0,
                            message="http 200")

    monkeypatch.setitem(health._BACKEND_HEALTH, "docker-compose", fake_health)
    out = health.check_services([handle])
    assert len(out) == 1 and out[0].status == "ok"


def test_check_services_times_out(monkeypatch):
    handle = ServiceHandle(name="api", backend="docker-compose",
                           health_url="GET http://localhost:1/h",
                           extra={"compose": "x.yml"})

    fake_clock = [0.0]
    def now():
        fake_clock[0] += 31
        return fake_clock[0]

    def fake_health(h, _http=None):
        return HealthStatus(ok=False, name=h.name, backend=h.backend, message="boom")

    monkeypatch.setitem(health._BACKEND_HEALTH, "docker-compose", fake_health)
    out = health.check_services([handle], _now=now, _sleep=lambda s: None)
    assert out[0].status == "fail"
    assert "boom" in out[0].message or "timeout" in out[0].message


def test_aggregate_all_ok(monkeypatch):
    handle = ServiceHandle(name="api", backend="docker-compose",
                           health_url="GET http://localhost:1/h",
                           extra={"compose": "x.yml"})

    def fake_health(h, _http=None):
        return HealthStatus(ok=True, name=h.name, backend=h.backend,
                            latency_ms=2.0, message="http 200")
    monkeypatch.setitem(health._BACKEND_HEALTH, "docker-compose", fake_health)

    runner = _ok_runner_for_version("ripgrep 14.0.3\n")
    cron = [CronHandle(name="nightly", backend="hermes", schedule="0 3 * * *", cmd="x")]
    report = health.aggregate([handle], [{"name": "rg", "minimum": "rg>=13"}], cron,
                              _runner=runner)
    assert report.ok
    assert {c.type for c in report.checks} == {"service", "binary", "cron"}


def test_aggregate_fails_on_first_timeout(monkeypatch):
    handle = ServiceHandle(name="api", backend="docker-compose",
                           health_url="GET http://localhost:1/h",
                           extra={"compose": "x.yml"})

    def fake_health(h, _http=None):
        return HealthStatus(ok=False, name=h.name, backend=h.backend, message="down")
    monkeypatch.setitem(health._BACKEND_HEALTH, "docker-compose", fake_health)

    fake_clock = [0.0]
    def now():
        fake_clock[0] += 40
        return fake_clock[0]

    runner = _ok_runner_for_version("ripgrep 14.0.3\n")
    report = health.aggregate(
        [handle], [{"name": "rg", "minimum": "rg>=13"}], [],
        _runner=runner, _now=now, _sleep=lambda s: None,
    )
    assert not report.ok
