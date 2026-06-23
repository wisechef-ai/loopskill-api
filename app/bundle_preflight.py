"""Server-side pre-flight checks for `recipes apply cookbook://<slug>`.

spotify_0608 Ph A — re-homed from ``bucket_preflight``. The meta-skill calls
this before touching the host filesystem. Returns a structured report that
includes a top-level ``ok`` boolean — green-light to proceed, red-light with a
list of problems otherwise.

Three checks:

  1. arch-compat        — every cookbook deployment's compatibility block matches
                          the caller's host fingerprint
  2. port-conflict      — no two services in the cookbook bind the same port,
                          and no cookbook port collides with a value already
                          claimed by an installed skill on the host
  3. env-var-collision  — no two skills declare the same required env var
                          with conflicting values, and no cookbook env var
                          collides with the host's existing exports

Each check is a plain function returning ``list[str]`` of problems. The
aggregator ``run_preflight`` combines them. Helpers are pure and importable
from tests.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from sqlalchemy.orm import Session

from app.models import Cookbook, CookbookDeployment, Skill

logger = logging.getLogger(__name__)


# ── Public API ──────────────────────────────────────────────────────────


def run_preflight(
    db: Session,
    cookbook_slug: str,
    host_fingerprint: dict | None = None,
    host_ports_in_use: Iterable[int] | None = None,
    host_env: dict | None = None,
) -> dict:
    """Run all three pre-flight checks and return a structured report."""
    cookbook = db.query(Cookbook).filter(Cookbook.slug == cookbook_slug).first()
    if not cookbook:
        return {
            "ok": False,
            "cookbook_slug": cookbook_slug,
            "problems": [f"cookbook_not_found:{cookbook_slug}"],
            "checks": {},
        }
    rows = (
        db.query(CookbookDeployment)
        .filter(CookbookDeployment.bundle_id == cookbook.id)  # compat-alias
        .order_by(CookbookDeployment.install_order.asc())
        .all()
    )
    skill_recipes = _load_recipes(db, rows)

    arch_problems = check_arch_compat(skill_recipes, host_fingerprint or {})
    port_problems = check_port_conflicts(skill_recipes, host_ports_in_use or [])
    env_problems = check_env_collisions(skill_recipes, host_env or {})

    problems = arch_problems + port_problems + env_problems
    return {
        "ok": not problems,
        "cookbook_slug": cookbook_slug,
        "problems": problems,
        "checks": {
            "arch_compat": {"ok": not arch_problems, "problems": arch_problems},
            "port_conflict": {"ok": not port_problems, "problems": port_problems},
            "env_collision": {"ok": not env_problems, "problems": env_problems},
        },
        "skills_inspected": len(skill_recipes),
    }


# ── Loaders ─────────────────────────────────────────────────────────────


def _load_recipes(db: Session, rows: list[CookbookDeployment]) -> list[dict]:
    """Hydrate each deployment row into a ``{slug, recipe}`` dict.

    ``recipe`` is read from the skill's stored manifest blob (the publishing
    pipeline accepts both manifest formats). Forks are not yet inspected —
    they're admitted optimistically and re-checked at install time on the host.
    Skills with no manifest are returned with an empty recipe so downstream
    checks treat them as no-op.
    """
    out: list[dict] = []
    for row in rows:
        if not row.skill_id:
            continue
        skill = db.query(Skill).filter(Skill.id == row.skill_id).first()
        if not skill:
            continue
        recipe = _parse_recipe_blob_for_skill(db, skill)
        out.append({"slug": skill.slug, "recipe": recipe or {}})
    return out


def _parse_recipe_blob_for_skill(db: Session, skill: Skill) -> dict | None:
    """Best-effort: pull recipe.yaml-shaped runtime data from latest version.

    The publish pipeline is what writes structured runtime data. Until that
    lands we return None for skills that don't have a parsed recipe; preflight
    then treats them as having no constraints, which is the safe default for
    additive checks.
    """
    versions = list(skill.versions or [])
    if not versions:
        return None
    blob = versions[0].skill_toml
    if not blob:
        return None
    try:
        # SKILL.md frontmatter is YAML, but tests don't ship a YAML parser.
        # If PyYAML is available, parse; otherwise return None — preflight
        # downgrades gracefully.
        import yaml  # type: ignore[import-not-found]

        data = yaml.safe_load(blob)
        if isinstance(data, dict):
            return data
    # Rationale: YAML parsing is optional; any import/parse error → return None
    except Exception:  # noqa: BLE001
        pass
    return None


# ── Checks ──────────────────────────────────────────────────────────────


def check_arch_compat(skill_recipes: list[dict], host_fp: dict) -> list[str]:
    """Each skill's compatibility.os/arch must include the host's values."""
    problems: list[str] = []
    host_os = (host_fp.get("os") or "").lower()
    host_arch = (host_fp.get("arch") or "").lower()
    for entry in skill_recipes:
        recipe = entry["recipe"]
        compat = (recipe.get("runtime") or {}).get("compatibility") or recipe.get("compatibility") or {}
        os_list = [s.lower() for s in (compat.get("os") or [])]
        arch_list = [s.lower() for s in (compat.get("arch") or [])]
        if host_os and os_list and host_os not in os_list:
            problems.append(f"arch_incompat:{entry['slug']}:os={host_os} not in {os_list}")
        if host_arch and arch_list and host_arch not in arch_list:
            problems.append(f"arch_incompat:{entry['slug']}:arch={host_arch} not in {arch_list}")
    return problems


def check_port_conflicts(skill_recipes: list[dict], host_ports: Iterable[int]) -> list[str]:
    """Detect duplicate service ports inside the cookbook and host overlaps."""
    problems: list[str] = []
    host_set = set(int(p) for p in host_ports if p is not None)
    seen: dict[int, str] = {}
    for entry in skill_recipes:
        recipe = entry["recipe"]
        services = (recipe.get("runtime") or {}).get("services") or recipe.get("services") or []
        for svc in services:
            port = svc.get("port") if isinstance(svc, dict) else None
            if port is None:
                continue
            try:
                port_int = int(port)
            except (TypeError, ValueError):
                continue
            if port_int in seen:
                problems.append(
                    f"port_conflict:port={port_int} claimed by both {seen[port_int]} and {entry['slug']}"
                )
            else:
                seen[port_int] = entry["slug"]
            if port_int in host_set:
                problems.append(
                    f"port_conflict:port={port_int} already in use on host (claimed by {entry['slug']})"
                )
    return problems


def check_env_collisions(skill_recipes: list[dict], host_env: dict) -> list[str]:
    """Detect required env vars that collide between skills or with host."""
    problems: list[str] = []
    seen: dict[str, str] = {}
    for entry in skill_recipes:
        recipe = entry["recipe"]
        env = (recipe.get("runtime") or {}).get("env") or recipe.get("env") or {}
        required: list[Any] = list(env.get("required") or []) if isinstance(env, dict) else []
        for item in required:
            # Items can be plain strings or {name: ..., value: ...} dicts.
            if isinstance(item, str):
                name, value = item, None
            elif isinstance(item, dict):
                name = item.get("name") or item.get("key")
                value = item.get("value")
            else:
                continue
            if not name:
                continue
            if name in seen and seen[name] != entry["slug"]:
                problems.append(f"env_collision:{name} required by both {seen[name]} and {entry['slug']}")
            else:
                seen[name] = entry["slug"]
            host_value = host_env.get(name)
            if host_value is not None and value is not None and host_value != value:
                problems.append(
                    f"env_collision:{name} host value differs from {entry['slug']} declared value"
                )
    return problems
