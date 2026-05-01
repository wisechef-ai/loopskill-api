"""Validate the `runtime:` block of a skill's recipe.yaml (Phase F.1).

Checks the document against runtime/recipe_schema.json. Implemented by hand —
no jsonschema dependency — because we ship stdlib + httpx + PyYAML and nothing
else. The schema file is still authoritative for publishers / docs / IDEs.

    >>> validate("runtime:\\n  compatibility:\\n    os: [linux]\\n    arch: [x86_64]\\n    ram_gb: 4\\n    network: required\\n")
    {'ok': True, 'errors': []}
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

_SCHEMA_PATH = Path(__file__).with_name("recipe_schema.json")
_OS_VALUES = {"linux", "macos", "windows"}
_ARCH_VALUES = {"x86_64", "arm64", "aarch64"}
_SERVICE_TYPES = {"docker-compose", "systemd-user", "launchd"}
_NETWORK_VALUES = {"required", "optional", "none"}
_CHECK_LATEST = {"daily", "weekly", "monthly", "never"}
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(yaml_text: str) -> dict[str, Any]:
    """Parse and validate a recipe.yaml document.

    Returns ``{"ok": bool, "errors": [str, ...]}``. Errors point at the
    failing path (e.g. ``runtime.compatibility.os``) so publishers can
    pinpoint the issue.
    """
    errors: list[str] = []

    try:
        doc = yaml.safe_load(yaml_text) if yaml_text else None
    except yaml.YAMLError as exc:
        return {"ok": False, "errors": [f"yaml: {exc}"]}

    if doc is None:
        return {"ok": False, "errors": ["recipe.yaml is empty"]}
    if not isinstance(doc, dict):
        return {"ok": False, "errors": ["top-level must be a mapping"]}
    if "runtime" not in doc:
        return {"ok": False, "errors": ["missing top-level `runtime:` block"]}

    runtime = doc["runtime"]
    if not isinstance(runtime, dict):
        return {"ok": False, "errors": ["`runtime:` must be a mapping"]}

    _validate_runtime(runtime, errors)
    return {"ok": not errors, "errors": errors}


def _validate_runtime(runtime: dict[str, Any], errors: list[str]) -> None:
    allowed = {"binaries", "services", "env", "cron", "compatibility"}
    for key in runtime:
        if key not in allowed:
            errors.append(f"runtime: unknown key '{key}'")

    if "compatibility" not in runtime:
        errors.append("runtime.compatibility is required")
    else:
        _validate_compatibility(runtime["compatibility"], errors)

    for i, b in enumerate(runtime.get("binaries") or []):
        _validate_binary(b, f"runtime.binaries[{i}]", errors)

    for i, s in enumerate(runtime.get("services") or []):
        _validate_service(s, f"runtime.services[{i}]", errors)

    if "env" in runtime:
        _validate_env(runtime["env"], errors)

    for i, c in enumerate(runtime.get("cron") or []):
        _validate_cron(c, f"runtime.cron[{i}]", errors)


def _validate_compatibility(compat: Any, errors: list[str]) -> None:
    path = "runtime.compatibility"
    if not isinstance(compat, dict):
        errors.append(f"{path}: must be a mapping")
        return

    for key in ("os", "arch", "ram_gb", "network"):
        if key not in compat:
            errors.append(f"{path}.{key} is required")

    os_list = compat.get("os")
    if isinstance(os_list, list):
        if not os_list:
            errors.append(f"{path}.os must not be empty")
        for v in os_list:
            if v not in _OS_VALUES:
                errors.append(f"{path}.os has invalid value '{v}' (must be one of {sorted(_OS_VALUES)})")
    elif os_list is not None:
        errors.append(f"{path}.os must be a list")

    arch_list = compat.get("arch")
    if isinstance(arch_list, list):
        if not arch_list:
            errors.append(f"{path}.arch must not be empty")
        for v in arch_list:
            if v not in _ARCH_VALUES:
                errors.append(f"{path}.arch has invalid value '{v}' (must be one of {sorted(_ARCH_VALUES)})")
    elif arch_list is not None:
        errors.append(f"{path}.arch must be a list")

    ram = compat.get("ram_gb")
    if isinstance(ram, dict):
        if "minimum" not in ram:
            errors.append(f"{path}.ram_gb.minimum is required")
        for k, v in ram.items():
            if k not in {"minimum", "recommended"}:
                errors.append(f"{path}.ram_gb has unknown key '{k}'")
            elif not isinstance(v, (int, float)) or v < 0:
                errors.append(f"{path}.ram_gb.{k} must be non-negative number")
    elif isinstance(ram, (int, float)):
        if ram < 0:
            errors.append(f"{path}.ram_gb must be non-negative")
    elif ram is not None:
        errors.append(f"{path}.ram_gb must be a number or mapping")

    net = compat.get("network")
    if isinstance(net, str):
        if net not in _NETWORK_VALUES:
            errors.append(f"{path}.network must be one of {sorted(_NETWORK_VALUES)}")
    elif isinstance(net, bool):
        pass
    elif net is not None:
        errors.append(f"{path}.network must be string or boolean")

    if "gpu" in compat:
        _validate_gpu(compat["gpu"], errors)

    if "disk_gb" in compat:
        d = compat["disk_gb"]
        if not isinstance(d, (int, float)) or d < 0:
            errors.append(f"{path}.disk_gb must be a non-negative number")


def _validate_gpu(gpu: Any, errors: list[str]) -> None:
    path = "runtime.compatibility.gpu"
    if not isinstance(gpu, dict):
        errors.append(f"{path}: must be a mapping")
        return
    allowed = {"required", "preferred", "vram_gb", "cuda"}
    for k in gpu:
        if k not in allowed:
            errors.append(f"{path} has unknown key '{k}'")
    if "required" in gpu and not isinstance(gpu["required"], bool):
        errors.append(f"{path}.required must be a boolean")
    if "vram_gb" in gpu:
        v = gpu["vram_gb"]
        if not isinstance(v, (int, float)) or v < 0:
            errors.append(f"{path}.vram_gb must be a non-negative number")
    if "cuda" in gpu and not isinstance(gpu["cuda"], str):
        errors.append(f"{path}.cuda must be a string")
    if "preferred" in gpu and not isinstance(gpu["preferred"], str):
        errors.append(f"{path}.preferred must be a string")


def _validate_binary(b: Any, path: str, errors: list[str]) -> None:
    if not isinstance(b, dict):
        errors.append(f"{path}: must be a mapping")
        return
    allowed = {"name", "capability", "provides", "minimum", "version",
               "release_source", "check_latest", "sha256"}
    for k in b:
        if k not in allowed:
            errors.append(f"{path}: unknown key '{k}'")
    if "name" not in b and "capability" not in b:
        errors.append(f"{path}: must have either `name` or `capability`")
    if "check_latest" in b and b["check_latest"] not in _CHECK_LATEST:
        errors.append(f"{path}.check_latest must be one of {sorted(_CHECK_LATEST)}")
    if "sha256" in b and (not isinstance(b["sha256"], str) or not _SHA256_RE.match(b["sha256"])):
        errors.append(f"{path}.sha256 must be a 64-char hex string")
    if "provides" in b and not (isinstance(b["provides"], list) and all(isinstance(x, str) for x in b["provides"])):
        errors.append(f"{path}.provides must be a list of strings")


def _validate_service(s: Any, path: str, errors: list[str]) -> None:
    if not isinstance(s, dict):
        errors.append(f"{path}: must be a mapping")
        return
    allowed = {"name", "type", "compose", "unit", "plist", "port", "health"}
    for k in s:
        if k not in allowed:
            errors.append(f"{path}: unknown key '{k}'")
    if "name" not in s:
        errors.append(f"{path}.name is required")
    if "type" not in s:
        errors.append(f"{path}.type is required")
    elif s["type"] not in _SERVICE_TYPES:
        errors.append(f"{path}.type must be one of {sorted(_SERVICE_TYPES)}")
    if "port" in s:
        p = s["port"]
        if not isinstance(p, int) or not (1 <= p <= 65535):
            errors.append(f"{path}.port must be int 1..65535")


def _validate_env(env: Any, errors: list[str]) -> None:
    if not isinstance(env, dict):
        errors.append("runtime.env: must be a mapping")
        return
    for k in env:
        if k not in {"required", "optional"}:
            errors.append(f"runtime.env: unknown key '{k}'")
    for k in ("required", "optional"):
        if k in env:
            v = env[k]
            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                errors.append(f"runtime.env.{k} must be a list of strings")


def _validate_cron(c: Any, path: str, errors: list[str]) -> None:
    if not isinstance(c, dict):
        errors.append(f"{path}: must be a mapping")
        return
    for k in c:
        if k not in {"name", "schedule", "cmd"}:
            errors.append(f"{path}: unknown key '{k}'")
    for k in ("name", "schedule", "cmd"):
        if k not in c:
            errors.append(f"{path}.{k} is required")
        elif not isinstance(c[k], str) or not c[k]:
            errors.append(f"{path}.{k} must be a non-empty string")
