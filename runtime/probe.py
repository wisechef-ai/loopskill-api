"""Architecture-aware install probe (Phase F.7).

Probes the host on first run, caches a JSON fingerprint at
``~/.recipes/runtime/host_fingerprint.json`` with a 24h TTL.
At install time we compare the active fingerprint against the candidate
skill's ``runtime.compatibility`` block:
  * HARD INCOMPAT (os/arch/ram_minimum/disk/network/cuda/required-gpu)
    → refuse install, suggest a sibling via the graph endpoint
  * SOFT INCOMPAT (preferred-gpu missing, ram below recommended)
    → emit warning + proceed

The probe never runs ``nvidia-smi`` etc. directly in tests — every shell-out
goes through the ``_runner`` parameter so tests inject canned output.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from runtime.adapters.base import runtime_root


FINGERPRINT_TTL_S = 24 * 3600


@dataclass
class HostFingerprint:
    os: str       # "linux" | "macos" | "windows"
    arch: str     # "x86_64" | "arm64" | "aarch64"
    ram_gb: float
    disk_gb: float
    has_gpu: bool = False
    gpu_vendor: str | None = None
    gpu_vram_gb: float = 0.0
    cuda: str | None = None
    network: bool = True
    captured_at: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def hash(self) -> str:
        keys = (self.os, self.arch, round(self.ram_gb, 2), round(self.disk_gb, 1),
                self.has_gpu, self.gpu_vendor, round(self.gpu_vram_gb, 2),
                self.cuda, self.network)
        h = hashlib.sha256(repr(keys).encode("utf-8"))
        return h.hexdigest()[:16]


def fingerprint_path() -> Path:
    return runtime_root() / "host_fingerprint.json"


def _detect_os() -> str:
    s = platform.system().lower()
    if s == "darwin":
        return "macos"
    if s.startswith("linux"):
        return "linux"
    if s.startswith("win"):
        return "windows"
    return s


def _detect_arch() -> str:
    m = (platform.machine() or "").lower()
    if m in {"x86_64", "amd64"}:
        return "x86_64"
    if m in {"arm64", "aarch64"}:
        return "arm64"
    return m or "unknown"


def _probe_linux(_runner) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    try:
        cp = _runner(["uname", "-ms"], capture_output=True, text=True, check=False)
        raw["uname"] = cp.stdout.strip()
    except FileNotFoundError:
        raw["uname"] = ""
    try:
        cp = _runner(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                      "--format=csv,noheader,nounits"],
                     capture_output=True, text=True, check=False)
        raw["nvidia_smi"] = cp.stdout.strip() if cp.returncode == 0 else ""
    except FileNotFoundError:
        raw["nvidia_smi"] = ""
    try:
        meminfo = Path("/proc/meminfo").read_text() if Path("/proc/meminfo").exists() else ""
        raw["meminfo"] = meminfo
    except OSError:
        raw["meminfo"] = ""
    try:
        cp = _runner(["lscpu"], capture_output=True, text=True, check=False)
        raw["lscpu"] = cp.stdout if cp.returncode == 0 else ""
    except FileNotFoundError:
        raw["lscpu"] = ""
    return raw


def _probe_macos(_runner) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    try:
        cp = _runner(["sysctl", "machdep.cpu"], capture_output=True, text=True, check=False)
        raw["cpu"] = cp.stdout
    except FileNotFoundError:
        raw["cpu"] = ""
    try:
        cp = _runner(["sysctl", "hw.memsize"], capture_output=True, text=True, check=False)
        raw["memsize"] = cp.stdout
    except FileNotFoundError:
        raw["memsize"] = ""
    try:
        cp = _runner(["system_profiler", "SPDisplaysDataType"],
                     capture_output=True, text=True, check=False)
        raw["displays"] = cp.stdout
    except FileNotFoundError:
        raw["displays"] = ""
    return raw


def _probe_windows(_runner) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    try:
        cp = _runner(["wmic", "cpu", "get", "Name,NumberOfCores"],
                     capture_output=True, text=True, check=False)
        raw["wmic_cpu"] = cp.stdout
    except FileNotFoundError:
        raw["wmic_cpu"] = ""
    try:
        cp = _runner(["wmic", "computersystem", "get", "TotalPhysicalMemory"],
                     capture_output=True, text=True, check=False)
        raw["wmic_mem"] = cp.stdout
    except FileNotFoundError:
        raw["wmic_mem"] = ""
    try:
        cp = _runner(["nvidia-smi", "--query-gpu=name,memory.total",
                      "--format=csv,noheader,nounits"],
                     capture_output=True, text=True, check=False)
        raw["nvidia_smi"] = cp.stdout if cp.returncode == 0 else ""
    except FileNotFoundError:
        raw["nvidia_smi"] = ""
    return raw


def _ram_gb_from_meminfo(meminfo: str) -> float:
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            try:
                kb = int(line.split()[1])
                return round(kb / 1024 / 1024, 2)
            except (IndexError, ValueError):
                return 0.0
    return 0.0


def _ram_gb_from_macos(memsize: str) -> float:
    for tok in memsize.split():
        if tok.isdigit():
            return round(int(tok) / 1024 / 1024 / 1024, 2)
    return 0.0


def _ram_gb_from_windows(wmic_mem: str) -> float:
    for tok in wmic_mem.split():
        if tok.isdigit() and len(tok) > 6:
            return round(int(tok) / 1024 / 1024 / 1024, 2)
    return 0.0


def _parse_nvidia(output: str) -> tuple[bool, str | None, float]:
    """('Tesla T4, 16384, 535.104.05') → (True, 'nvidia', 16.0)."""
    line = (output or "").splitlines()[0] if output else ""
    if not line:
        return False, None, 0.0
    parts = [p.strip() for p in line.split(",")]
    vram = 0.0
    if len(parts) >= 2:
        try:
            vram = round(float(parts[1]) / 1024, 2)
        except ValueError:
            vram = 0.0
    return True, "nvidia", vram


def _disk_gb() -> float:
    try:
        import shutil as _sh
        usage = _sh.disk_usage(str(Path.home()))
        return round(usage.free / 1024 / 1024 / 1024, 1)
    except OSError:
        return 0.0


def probe(*, _runner=subprocess.run, _now=time.time) -> HostFingerprint:
    os_name = _detect_os()
    arch = _detect_arch()

    if os_name == "linux":
        raw = _probe_linux(_runner)
        ram = _ram_gb_from_meminfo(raw.get("meminfo", ""))
    elif os_name == "macos":
        raw = _probe_macos(_runner)
        ram = _ram_gb_from_macos(raw.get("memsize", ""))
    elif os_name == "windows":
        raw = _probe_windows(_runner)
        ram = _ram_gb_from_windows(raw.get("wmic_mem", ""))
    else:
        raw = {}
        ram = 0.0

    has_gpu, vendor, vram = _parse_nvidia(raw.get("nvidia_smi", ""))

    fp = HostFingerprint(
        os=os_name, arch=arch, ram_gb=ram, disk_gb=_disk_gb(),
        has_gpu=has_gpu, gpu_vendor=vendor, gpu_vram_gb=vram,
        cuda=None, network=True, captured_at=int(_now()), raw=raw,
    )
    return fp


def load_or_probe(*, ttl_s: int = FINGERPRINT_TTL_S, _runner=subprocess.run,
                  _now=time.time) -> HostFingerprint:
    p = fingerprint_path()
    if p.exists():
        try:
            data = json.loads(p.read_text())
            if _now() - int(data.get("captured_at", 0)) < ttl_s:
                return HostFingerprint(**data)
        except (json.JSONDecodeError, TypeError):
            pass

    fp = probe(_runner=_runner, _now=_now)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(asdict(fp), indent=2, default=str))
    return fp


# ---------------------------------------------------------------------------
# Compatibility comparison
# ---------------------------------------------------------------------------


@dataclass
class CompatResult:
    decision: str  # "ok" | "soft_incompat" | "hard_incompat"
    hard: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    fingerprint_hash: str = ""
    suggested_alternative: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def compare(fp: HostFingerprint, compatibility: dict[str, Any]) -> CompatResult:
    hard: list[str] = []
    soft: list[str] = []

    os_list = compatibility.get("os") or []
    if os_list and fp.os not in os_list:
        hard.append(f"host os '{fp.os}' not in supported {os_list}")

    arch_list = compatibility.get("arch") or []
    if arch_list and fp.arch not in arch_list:
        hard.append(f"host arch '{fp.arch}' not in supported {arch_list}")

    ram = compatibility.get("ram_gb")
    if isinstance(ram, dict):
        minimum = float(ram.get("minimum", 0))
        recommended = float(ram.get("recommended", minimum))
    elif isinstance(ram, (int, float)):
        minimum = float(ram)
        recommended = minimum
    else:
        minimum = recommended = 0.0
    if fp.ram_gb and minimum and fp.ram_gb < minimum:
        hard.append(f"ram {fp.ram_gb} GB < required {minimum} GB")
    elif fp.ram_gb and recommended and fp.ram_gb < recommended:
        soft.append(f"ram {fp.ram_gb} GB < recommended {recommended} GB")

    disk = compatibility.get("disk_gb")
    if disk and fp.disk_gb and fp.disk_gb < float(disk):
        hard.append(f"free disk {fp.disk_gb} GB < required {disk} GB")

    network = compatibility.get("network")
    if network in ("required", True) and not fp.network:
        hard.append("skill requires network but host fingerprint says offline")

    gpu = compatibility.get("gpu") or {}
    if gpu.get("required") and not fp.has_gpu:
        hard.append("skill requires GPU; host has none")
    elif gpu.get("preferred") and not fp.has_gpu:
        soft.append(f"skill prefers {gpu.get('preferred')} GPU; host has none — expect slower runs")
    if gpu.get("vram_gb") and fp.has_gpu:
        if fp.gpu_vram_gb < float(gpu["vram_gb"]):
            (hard if gpu.get("required") else soft).append(
                f"GPU has {fp.gpu_vram_gb} GB VRAM < {gpu['vram_gb']} GB requested"
            )

    decision = "hard_incompat" if hard else ("soft_incompat" if soft else "ok")
    return CompatResult(decision=decision, hard=hard, soft=soft,
                        fingerprint_hash=fp.hash())


# ---------------------------------------------------------------------------
# Refuse install + suggest alternative via graph endpoint
# ---------------------------------------------------------------------------


def suggest_alternative(skill_slug: str, *, base_url: str | None = None,
                        _http=None) -> str | None:
    """Ask the B.5 graph endpoint for a ``replaced_by`` alternative.

    Falls back to ``None`` when the endpoint is missing — that's the path
    the plan calls out for hosts running an older API.
    """
    if base_url is None:
        import os
        base_url = os.environ.get("RECIPES_API_URL", "https://recipes.wisechef.ai")
    if _http is None:
        import httpx
        _http = httpx
    try:
        r = _http.get(f"{base_url.rstrip('/')}/api/graph/related",
                      params={"skill": skill_slug, "edge": "replaced_by"},
                      timeout=3.0)
    except Exception:
        return None
    if getattr(r, "status_code", 0) != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    items = data.get("items") or data.get("related") or []
    for item in items:
        if isinstance(item, dict) and item.get("slug"):
            return item["slug"]
        if isinstance(item, str):
            return item
    return None


def refuse_or_warn(skill_slug: str, fp: HostFingerprint,
                   compatibility: dict[str, Any], *, _http=None) -> dict[str, Any]:
    """Top-level entry point: decide install/refuse, optionally suggest swap."""
    result = compare(fp, compatibility)
    out: dict[str, Any] = result.as_dict()
    out["skill_slug"] = skill_slug
    if result.decision == "hard_incompat":
        out["suggested_alternative"] = suggest_alternative(skill_slug, _http=_http)
    return out
