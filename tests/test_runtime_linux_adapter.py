"""F.2 — Linux adapter resolver picks apt/dnf/pacman over curl fallback."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from runtime.adapters import linux as linux_adapter


@pytest.fixture
def runtime_root(tmp_path, monkeypatch):
    monkeypatch.setenv("RECIPES_RUNTIME_ROOT", str(tmp_path / "rr"))
    return tmp_path / "rr"


def test_resolve_prefers_apt(monkeypatch):
    monkeypatch.setattr(linux_adapter, "which", lambda n: "/usr/bin/apt" if n == "apt" else None)
    plan = linux_adapter.resolve({"name": "ripgrep"})
    assert plan.method == "apt"
    assert plan.package == "ripgrep"


def test_resolve_falls_back_to_curl(monkeypatch, runtime_root):
    monkeypatch.setattr(linux_adapter, "which", lambda n: None)
    plan = linux_adapter.resolve({
        "name": "uv", "url": "https://example.com/uv",
        "sha256": "0" * 64, "_skill_slug": "demo",
    })
    assert plan.method == "curl"
    assert plan.url == "https://example.com/uv"


def test_apt_install_invokes_apt(monkeypatch):
    monkeypatch.setattr(linux_adapter, "which", lambda n: "/usr/bin/apt")
    plan = linux_adapter.resolve({"name": "ripgrep"})

    seen = {}
    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")
    res = linux_adapter.install(plan, _runner=fake_run)
    assert res.ok
    assert "apt-get" in seen["cmd"]
    assert "ripgrep" in seen["cmd"]


def test_curl_requires_sha256(monkeypatch, runtime_root):
    monkeypatch.setattr(linux_adapter, "which", lambda n: None)
    plan = linux_adapter.resolve({
        "name": "tool", "url": "https://example.com/tool",
        "_skill_slug": "demo",
    })
    res = linux_adapter.install(plan, _runner=None)
    assert not res.ok
    assert "sha256" in res.message


def test_curl_validates_sha256(monkeypatch, runtime_root):
    monkeypatch.setattr(linux_adapter, "which", lambda n: None)
    payload = b"#!/bin/sh\necho hi\n"
    real_sha = hashlib.sha256(payload).hexdigest()

    class _R:
        status_code = 200
        content = payload

    class _HTTP:
        @staticmethod
        def get(url, **kw):
            return _R()

    plan = linux_adapter.resolve({
        "name": "tool", "url": "https://example.com/tool",
        "sha256": real_sha, "_skill_slug": "demo",
    })
    res = linux_adapter.install(plan, _http=_HTTP)
    assert res.ok, res.message
    assert plan.target_path.exists()
    assert plan.target_path.read_bytes() == payload


def test_curl_rejects_sha256_mismatch(monkeypatch, runtime_root):
    monkeypatch.setattr(linux_adapter, "which", lambda n: None)

    class _R:
        status_code = 200
        content = b"different bytes"

    class _HTTP:
        @staticmethod
        def get(url, **kw):
            return _R()

    plan = linux_adapter.resolve({
        "name": "tool", "url": "https://example.com/tool",
        "sha256": "f" * 64, "_skill_slug": "demo",
    })
    res = linux_adapter.install(plan, _http=_HTTP)
    assert not res.ok
    assert "sha256 mismatch" in res.message
