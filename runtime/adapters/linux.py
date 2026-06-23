"""Linux adapter — apt → dnf → pacman → curl-sha256 fallback."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .base import AdapterPlan, InstallResult, skill_root, which

_PKG_MANAGERS = ("apt", "dnf", "pacman")


def _detect_pkg_manager() -> str | None:
    for pm in _PKG_MANAGERS:
        if which(pm):
            return pm
    return None


def resolve(binary_spec: dict[str, Any]) -> AdapterPlan:
    """Pick the install method for a binary spec.

    Resolution order:
      1. If a system package manager is available, prefer it (apt > dnf > pacman).
      2. Otherwise fall back to curl + sha256 (if release_source/url present).
    """
    name = binary_spec.get("name") or (binary_spec.get("provides") or ["?"])[0]
    package = binary_spec.get("package") or name

    pm = _detect_pkg_manager()
    if pm:
        return AdapterPlan(name=name, method=pm, package=package)

    url = binary_spec.get("url") or binary_spec.get("release_source")
    sha = binary_spec.get("sha256")
    return AdapterPlan(
        name=name,
        method="curl",
        url=url,
        sha256=sha,
        target_path=skill_root(binary_spec.get("_skill_slug", "_unscoped")) / "bin" / name,
    )


def _run(cmd: list[str], _runner=subprocess.run) -> subprocess.CompletedProcess:
    """Indirected so tests can monkeypatch."""
    return _runner(cmd, capture_output=True, text=True, check=False)


def install(plan: AdapterPlan, _runner=subprocess.run, _http=None) -> InstallResult:
    if plan.method == "apt":
        cp = _run(["sudo", "-n", "apt-get", "install", "-y", plan.package or plan.name], _runner)
        ok = cp.returncode == 0
        return InstallResult(ok=ok, name=plan.name, method="apt", message=cp.stderr or cp.stdout)
    if plan.method == "dnf":
        cp = _run(["sudo", "-n", "dnf", "install", "-y", plan.package or plan.name], _runner)
        return InstallResult(ok=cp.returncode == 0, name=plan.name, method="dnf",
                             message=cp.stderr or cp.stdout)
    if plan.method == "pacman":
        cp = _run(["sudo", "-n", "pacman", "-S", "--noconfirm", plan.package or plan.name], _runner)
        return InstallResult(ok=cp.returncode == 0, name=plan.name, method="pacman",
                             message=cp.stderr or cp.stdout)
    if plan.method == "curl":
        return _curl_sha256(plan, _http=_http)
    return InstallResult(ok=False, name=plan.name, method=plan.method,
                         message=f"unknown method '{plan.method}'")


def _curl_sha256(plan: AdapterPlan, _http=None) -> InstallResult:
    if not plan.url:
        return InstallResult(ok=False, name=plan.name, method="curl",
                             message="no url in plan")
    if not plan.sha256:
        return InstallResult(ok=False, name=plan.name, method="curl",
                             message="curl fallback requires sha256 (no curl|bash)")
    target = plan.target_path or (skill_root("_unscoped") / "bin" / plan.name)
    target.parent.mkdir(parents=True, exist_ok=True)

    if _http is None:
        import httpx
        _http = httpx
    try:
        r = _http.get(plan.url, follow_redirects=True, timeout=30.0)
    except Exception as exc:  # network failure → install fails (caller rolls back)
        return InstallResult(ok=False, name=plan.name, method="curl", message=str(exc))
    status = getattr(r, "status_code", 0)
    if status != 200:
        return InstallResult(ok=False, name=plan.name, method="curl",
                             message=f"http {status}")
    data = getattr(r, "content", b"")
    actual = hashlib.sha256(data).hexdigest()
    if actual.lower() != plan.sha256.lower():
        return InstallResult(ok=False, name=plan.name, method="curl",
                             message=f"sha256 mismatch (expected {plan.sha256}, got {actual})")
    target.write_bytes(data)
    target.chmod(0o755)
    _record(target.parent.parent, plan)
    return InstallResult(ok=True, name=plan.name, method="curl", path=target)


def _record(skill_dir: Path, plan: AdapterPlan) -> None:
    """Drop a manifest entry so uninstall can find what we wrote."""
    manifest = skill_dir / "installed.json"
    entries = []
    if manifest.exists():
        try:
            entries = json.loads(manifest.read_text())
        except json.JSONDecodeError:
            entries = []
    entries.append({"name": plan.name, "method": plan.method,
                    "path": str(plan.target_path) if plan.target_path else None})
    manifest.write_text(json.dumps(entries, indent=2))


def uninstall(name: str, _runner=subprocess.run) -> bool:
    """Remove via apt/dnf/pacman, or by deleting per-skill curl artifacts.

    For curl-installed binaries we walk every per-skill manifest; for system
    packages we shell out to the detected manager.
    """
    pm = _detect_pkg_manager()
    removed_any = False

    root = skill_root("_unscoped").parent
    if root.exists():
        for skill_dir in root.iterdir():
            manifest = skill_dir / "installed.json"
            if not manifest.exists():
                continue
            try:
                entries = json.loads(manifest.read_text())
            except json.JSONDecodeError:
                continue
            kept = []
            for e in entries:
                if e.get("name") == name and e.get("method") == "curl":
                    if e.get("path"):
                        Path(e["path"]).unlink(missing_ok=True)
                    removed_any = True
                else:
                    kept.append(e)
            manifest.write_text(json.dumps(kept, indent=2))

    if pm == "apt":
        cp = _run(["sudo", "-n", "apt-get", "remove", "-y", name], _runner)
        if cp.returncode == 0:
            removed_any = True
    elif pm == "dnf":
        cp = _run(["sudo", "-n", "dnf", "remove", "-y", name], _runner)
        if cp.returncode == 0:
            removed_any = True
    elif pm == "pacman":
        cp = _run(["sudo", "-n", "pacman", "-R", "--noconfirm", name], _runner)
        if cp.returncode == 0:
            removed_any = True

    return removed_any
