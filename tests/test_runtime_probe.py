"""F.7 — probe: arch-incompat refusal with alt suggestion."""

from __future__ import annotations

import subprocess

import pytest

from runtime import probe as probe_mod


@pytest.fixture
def runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RECIPES_RUNTIME_ROOT", str(tmp_path / "rr"))
    return tmp_path / "rr"


def _fake_runner(map_):
    """Return a subprocess.run-compatible callable driven by ``map_``.

    Keys are the first arg of the command; values are dicts with ``stdout``,
    ``stderr``, and ``returncode``.
    """
    def run(cmd, **kw):
        head = cmd[0] if cmd else ""
        out = map_.get(head, {})
        return subprocess.CompletedProcess(
            cmd, out.get("returncode", 0),
            stdout=out.get("stdout", ""), stderr=out.get("stderr", ""),
        )
    return run


def test_probe_linux_x86_64(monkeypatch, runtime_dir):
    monkeypatch.setattr(probe_mod, "_detect_os", lambda: "linux")
    monkeypatch.setattr(probe_mod, "_detect_arch", lambda: "x86_64")
    monkeypatch.setattr(
        probe_mod.Path, "exists",
        lambda self: False if str(self) == "/proc/meminfo" else False,
    )
    runner = _fake_runner({
        "uname": {"stdout": "Linux x86_64\n"},
        "nvidia-smi": {"stdout": "Tesla T4, 16384, 535.0\n"},
        "lscpu": {"stdout": "Architecture: x86_64\n"},
    })
    fp = probe_mod.probe(_runner=runner, _now=lambda: 1700000000)
    assert fp.os == "linux"
    assert fp.arch == "x86_64"
    assert fp.has_gpu
    assert fp.gpu_vendor == "nvidia"
    assert fp.gpu_vram_gb == 16.0


def test_compare_hard_incompat_arch():
    fp = probe_mod.HostFingerprint(os="linux", arch="x86_64", ram_gb=8, disk_gb=20,
                                   network=True)
    res = probe_mod.compare(fp, {"os": ["linux"], "arch": ["arm64"],
                                 "ram_gb": 4, "network": "required"})
    assert res.decision == "hard_incompat"
    assert any("arch" in h for h in res.hard)


def test_compare_soft_incompat_gpu_preferred():
    fp = probe_mod.HostFingerprint(os="linux", arch="x86_64", ram_gb=8, disk_gb=20,
                                   has_gpu=False, network=True)
    res = probe_mod.compare(fp, {
        "os": ["linux"], "arch": ["x86_64"],
        "gpu": {"required": False, "preferred": "nvidia", "vram_gb": 8},
        "ram_gb": 4, "network": "required",
    })
    assert res.decision == "soft_incompat"
    assert any("GPU" in s for s in res.soft)


def test_compare_ram_minimum_violation():
    fp = probe_mod.HostFingerprint(os="linux", arch="x86_64", ram_gb=2, disk_gb=20,
                                   network=True)
    res = probe_mod.compare(fp, {"os": ["linux"], "arch": ["x86_64"],
                                 "ram_gb": {"minimum": 4, "recommended": 8},
                                 "network": "required"})
    assert res.decision == "hard_incompat"


def test_compare_ok():
    fp = probe_mod.HostFingerprint(os="linux", arch="x86_64", ram_gb=8, disk_gb=20,
                                   network=True)
    res = probe_mod.compare(fp, {"os": ["linux"], "arch": ["x86_64"],
                                 "ram_gb": 4, "network": "required"})
    assert res.decision == "ok"


def test_refuse_or_warn_suggests_alternative(monkeypatch):
    fp = probe_mod.HostFingerprint(os="linux", arch="x86_64", ram_gb=8,
                                   disk_gb=20, network=True)

    class _R:
        status_code = 200
        @staticmethod
        def json():
            return {"items": [{"slug": "alt-skill"}]}

    class _HTTP:
        @staticmethod
        def get(url, **kw):
            return _R()

    out = probe_mod.refuse_or_warn(
        "primary", fp,
        {"os": ["macos"], "arch": ["arm64"], "ram_gb": 4, "network": "required"},
        _http=_HTTP,
    )
    assert out["decision"] == "hard_incompat"
    assert out["suggested_alternative"] == "alt-skill"


def test_refuse_or_warn_handles_missing_endpoint(monkeypatch):
    fp = probe_mod.HostFingerprint(os="linux", arch="x86_64", ram_gb=8,
                                   disk_gb=20, network=True)

    class _HTTP:
        @staticmethod
        def get(url, **kw):
            raise RuntimeError("connection refused")

    out = probe_mod.refuse_or_warn(
        "primary", fp,
        {"os": ["macos"], "arch": ["arm64"], "ram_gb": 4, "network": "required"},
        _http=_HTTP,
    )
    assert out["decision"] == "hard_incompat"
    assert out["suggested_alternative"] is None


def test_load_or_probe_caches(runtime_dir, monkeypatch):
    monkeypatch.setattr(probe_mod, "_detect_os", lambda: "linux")
    monkeypatch.setattr(probe_mod, "_detect_arch", lambda: "x86_64")
    runner = _fake_runner({"nvidia-smi": {"returncode": 1}})
    calls = {"n": 0}

    def counting_runner(cmd, **kw):
        calls["n"] += 1
        return runner(cmd, **kw)

    fp1 = probe_mod.load_or_probe(_runner=counting_runner, _now=lambda: 1000)
    n_after_first = calls["n"]
    fp2 = probe_mod.load_or_probe(_runner=counting_runner, _now=lambda: 2000)
    assert calls["n"] == n_after_first  # second call hit the cache
    assert fp1.captured_at == fp2.captured_at
