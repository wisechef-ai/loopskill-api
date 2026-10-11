"""Every repo file the app reads at runtime must trigger a deploy when it changes.

perclient_1110 (2026-10-12): #403 changed docs/recipes-skill/SKILL.md, CI went
green, deploy.yml's paths filter skipped it, and /skill kept serving the old text.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _deploy_paths() -> list[str]:
    wf = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))
    return on["push"]["paths"]


def _covered(rel: str, patterns: list[str]) -> bool:
    for pat in patterns:
        rx = "^" + re.escape(pat).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$"
        if re.match(rx, rel):
            return True
    return False


def test_runtime_served_docs_trigger_deploy():
    serve_modules = [ROOT / "app/skill_serve_routes.py", ROOT / "app/fleet_skill_serve_routes.py"]
    served = set()
    for m in serve_modules:
        served |= set(re.findall(r"docs/[A-Za-z0-9_\-/]+\.md", m.read_text(encoding="utf-8")))
    assert {"docs/recipes-skill/SKILL.md", "docs/fleet-skill/SKILL.md"} <= served
    paths = _deploy_paths()
    missing = sorted(f for f in served if not _covered(f, paths))
    assert missing == [], f"runtime-served docs not in deploy.yml paths: {missing}"
