"""W0.2 (integrator_2905) — file-size discipline regression.

Pins the two invariants the pyfile-size-check CI gate enforces, so a future
edit that re-bloats a guarded module trips a fast local test instead of only
the (currently runner-blocked) CI job:

  1. The middleware + MCP packages — the god-objects topshelf_2605/J explicitly
     split — must stay <= 600 lines. This is a HARD gate, never waived. W0.1
     pushed api_key.py to 611; W0.2 extracted fleet-key resolution into
     app/middleware/_token_auth.py to restore headroom.

  2. The 5 legacy modules above 600 are a CLOSED waiver set. No NEW app/ module
     may exceed 600 — that would be exactly the god-object regression the gate
     exists to stop. The waiver list here mirrors the allowlist in
     .github/workflows/pyfile-size-check.yml; the two must agree.
"""
from __future__ import annotations

from pathlib import Path

import pytest

THRESHOLD = 600

# Mirror of the waiver allowlist in .github/workflows/pyfile-size-check.yml.
# Each is a pre-existing legacy module tracked for split in a later workstream.
LEGACY_WAIVER = {
    "app/models.py",
    "app/subscription_service.py",
    "app/cookbook_routes.py",
    "app/skill_routes.py",
    "app/graph_extension.py",
}

REPO_ROOT = Path(__file__).resolve().parent.parent


def _line_count(p: Path) -> int:
    with p.open("rb") as fh:
        return sum(1 for _ in fh)


def _app_py_files() -> list[Path]:
    return [
        p
        for p in (REPO_ROOT / "app").rglob("*.py")
        if "__pycache__" not in p.parts
    ]


class TestMiddlewareMcpGodObjectGuard:
    """HARD gate — never waived: middleware + mcp packages stay modular."""

    def test_middleware_and_mcp_under_600(self):
        offenders = []
        for sub in ("middleware", "mcp"):
            base = REPO_ROOT / "app" / sub
            if not base.exists():
                continue
            for p in base.rglob("*.py"):
                if "__pycache__" in p.parts:
                    continue
                n = _line_count(p)
                if n > THRESHOLD:
                    offenders.append(f"{p.relative_to(REPO_ROOT)} = {n}")
        assert not offenders, (
            "middleware/mcp god-object guard tripped (>600 lines, NEVER waived): "
            + ", ".join(offenders)
        )


class TestNoNewGodObjects:
    """No NON-waived app/ module may exceed 600 lines."""

    def test_no_unwaived_oversized_modules(self):
        offenders = []
        for p in _app_py_files():
            rel = str(p.relative_to(REPO_ROOT))
            if rel in LEGACY_WAIVER:
                continue
            n = _line_count(p)
            if n > THRESHOLD:
                offenders.append(f"{rel} = {n}")
        assert not offenders, (
            "New oversized app/ module(s) — split them or (only for genuine "
            "legacy debt) add to the waiver in BOTH this test and "
            ".github/workflows/pyfile-size-check.yml: " + ", ".join(offenders)
        )

    def test_waiver_list_has_no_dead_entries(self):
        """A waiver entry that has dropped <=600 (or been deleted) must be
        removed — keep the waiver honest so it shrinks toward empty."""
        dead = []
        for rel in LEGACY_WAIVER:
            p = REPO_ROOT / rel
            if not p.exists():
                dead.append(f"{rel} (missing)")
                continue
            if _line_count(p) <= THRESHOLD:
                dead.append(f"{rel} (now <=600 — remove from waiver)")
        assert not dead, "Stale waiver entries: " + ", ".join(dead)
