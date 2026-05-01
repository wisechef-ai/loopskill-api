"""Shared adapter primitives — dataclasses, helpers, root path."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def runtime_root() -> Path:
    """``~/.recipes/runtime/`` — overridable via ``RECIPES_RUNTIME_ROOT`` for tests."""
    override = os.environ.get("RECIPES_RUNTIME_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".recipes" / "runtime"


def skill_root(skill_slug: str) -> Path:
    """Per-skill state directory under runtime_root()."""
    p = runtime_root() / skill_slug
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class AdapterPlan:
    """Output of resolve(): describes how a binary will be installed."""
    name: str
    method: str  # "apt" | "dnf" | "pacman" | "brew" | "winget" | "curl"
    package: str | None = None
    url: str | None = None
    sha256: str | None = None
    target_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class InstallResult:
    ok: bool
    name: str
    method: str
    message: str = ""
    path: Path | None = None


def which(name: str) -> str | None:
    """shutil.which but null-safe; allows monkeypatch in tests."""
    return shutil.which(name)
