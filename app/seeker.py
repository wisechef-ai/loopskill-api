"""Skill-Seeker service — cross-vendor local probe.

Walks the per-vendor skill directories on the local machine
(Claude, Codex, Hermes, OpenCode), parses each ``SKILL.md``
frontmatter, and diffs the result against the public catalog so
agents can recommend upgrades or surface missing skills.

READ-ONLY: never mutates vendor directories.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

logger = logging.getLogger("wiserecipes.seeker")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# Vendors we currently know how to probe.
_VENDORS = ("claude", "codex", "hermes", "opencode")


# ── Datamodels ──────────────────────────────────────────────────────────────


@dataclass
class InstalledSkill:
    """A SKILL.md found on disk under a vendor's skill directory."""

    vendor: str
    name: str
    version: str | None
    path: str
    description: str | None = None


@dataclass
class Recommendation:
    """A diff result between an installed skill and the public catalog."""

    vendor: str
    slug: str
    installed_version: str | None
    catalog_version: str | None
    reason: str  # "newer" | "better-quality" | "missing"


# ── Vendor-path resolution ──────────────────────────────────────────────────


def _linux_paths() -> dict[str, Path]:
    """Linux paths. Honor XDG_CONFIG_HOME when set."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        base = Path(xdg)
        return {v: base / v / "skills" for v in _VENDORS}
    home = Path.home()
    return {
        "claude": home / ".claude" / "skills",
        "codex": home / ".codex" / "skills",
        "hermes": home / ".hermes" / "skills",
        "opencode": home / ".opencode" / "skills",
    }


def _macos_paths() -> dict[str, Path]:
    """macOS uses ``~/Library/Application Support/<Vendor>/skills``."""
    base = Path.home() / "Library" / "Application Support"
    return {
        "claude": base / "Claude" / "skills",
        "codex": base / "Codex" / "skills",
        "hermes": base / "Hermes" / "skills",
        "opencode": base / "OpenCode" / "skills",
    }


def _windows_paths() -> dict[str, Path]:
    """Windows uses %APPDATA%/<Vendor>/skills."""
    appdata = os.environ.get("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return {
        "claude": base / "Claude" / "skills",
        "codex": base / "Codex" / "skills",
        "hermes": base / "Hermes" / "skills",
        "opencode": base / "OpenCode" / "skills",
    }


def vendor_paths(platform: str | None = None) -> dict[str, Path]:
    """Return the per-vendor skill directory map for the current platform.

    The ``platform`` arg is exposed for tests; production calls use
    ``sys.platform`` directly. Paths are returned even if they do not
    exist — callers should filter with ``Path.exists()``.
    """
    plat = platform if platform is not None else sys.platform
    if plat == "darwin":
        return _macos_paths()
    if plat.startswith("win"):
        return _windows_paths()
    return _linux_paths()


# ── Vendor scan ─────────────────────────────────────────────────────────────


def _parse_frontmatter(text: str) -> dict | None:
    m = _FRONTMATTER_RE.match(text.lstrip())
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    return meta


def _scan_skill_md(skill_md: Path, vendor: str) -> InstalledSkill | None:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        logger.warning("seeker: cannot read %s: %s", skill_md, exc)
        return None

    meta = _parse_frontmatter(text)
    if meta is None:
        logger.warning("seeker: malformed frontmatter in %s — skipping", skill_md)
        return None

    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning("seeker: missing 'name' in %s — skipping", skill_md)
        return None

    version = meta.get("version")
    if version is not None and not isinstance(version, str):
        version = str(version)

    description = meta.get("description")
    if description is not None and not isinstance(description, str):
        description = str(description)

    return InstalledSkill(
        vendor=vendor,
        name=name.strip(),
        version=version,
        path=str(skill_md.parent),
        description=description,
    )


def scan_vendor(path: Path, vendor: str | None = None) -> list[InstalledSkill]:
    """Walk ``path`` looking for ``SKILL.md`` files. READ-ONLY.

    Returns an empty list if the directory does not exist; logs and
    skips files that fail to parse.
    """
    if not path.exists() or not path.is_dir():
        return []

    vendor_name = vendor or path.parent.name.lower().lstrip(".")
    found: list[InstalledSkill] = []
    try:
        for skill_md in path.rglob("SKILL.md"):
            if not skill_md.is_file():
                continue
            entry = _scan_skill_md(skill_md, vendor_name)
            if entry is not None:
                found.append(entry)
    except OSError as exc:
        logger.warning("seeker: walk failed under %s: %s", path, exc)
    return found


# ── Diff against catalog ────────────────────────────────────────────────────


def _compare_versions(installed: str | None, catalog: str | None) -> int:
    """Returns -1 / 0 / 1 like ``cmp(installed, catalog)``.

    Falls back to string compare when ``packaging.version`` rejects the
    input (vendor authors are creative). ``None`` sorts before anything.
    """
    if installed == catalog:
        return 0
    if installed is None:
        return -1
    if catalog is None:
        return 1
    try:
        from packaging.version import InvalidVersion, Version

        try:
            a = Version(installed)
            b = Version(catalog)
        except InvalidVersion:
            return (installed > catalog) - (installed < catalog)
        return (a > b) - (a < b)
    except ImportError:  # pragma: no cover - packaging is a runtime dep
        return (installed > catalog) - (installed < catalog)


def _catalog_version(skill) -> str | None:
    """Best-effort latest semver for a Skill ORM row.

    Skill.versions is ordered newest-first by created_at; if there are
    no versions, return None and let the caller treat it as a no-op.
    """
    versions = getattr(skill, "versions", None) or []
    for v in versions:
        sem = getattr(v, "semver", None)
        if sem:
            return sem
    return None


def diff_against_catalog(
    installed: Iterable[InstalledSkill],
    catalog: Iterable,
) -> list[Recommendation]:
    """Compare local installs against public catalog rows.

    Emits a Recommendation per installed skill where:
      - reason="newer"          : catalog has a newer semver
      - reason="better-quality" : same version, but catalog has higher rating_avg
      - reason="missing"        : installed skill not in catalog
    Skills whose installed version equals (or beats) the catalog are
    silently dropped; they need no action.
    """
    catalog_by_slug = {getattr(s, "slug", None): s for s in catalog if getattr(s, "slug", None)}

    recs: list[Recommendation] = []
    for inst in installed:
        cat = catalog_by_slug.get(inst.name)
        if cat is None:
            recs.append(
                Recommendation(
                    vendor=inst.vendor,
                    slug=inst.name,
                    installed_version=inst.version,
                    catalog_version=None,
                    reason="missing",
                )
            )
            continue

        cat_version = _catalog_version(cat)
        cmp = _compare_versions(inst.version, cat_version)
        if cmp < 0:
            recs.append(
                Recommendation(
                    vendor=inst.vendor,
                    slug=inst.name,
                    installed_version=inst.version,
                    catalog_version=cat_version,
                    reason="newer",
                )
            )
            continue
        # Equal version — surface a recommendation only if the catalog's
        # quality signal (rating_avg) suggests there's a meaningful gap.
        if cmp == 0:
            rating = getattr(cat, "rating_avg", None)
            if rating is not None and rating >= 4.5:
                recs.append(
                    Recommendation(
                        vendor=inst.vendor,
                        slug=inst.name,
                        installed_version=inst.version,
                        catalog_version=cat_version,
                        reason="better-quality",
                    )
                )
    return recs


def recommendation_to_dict(rec: Recommendation) -> dict:
    """Serializer used by the MCP tool layer."""
    return asdict(rec)


def installed_to_dict(inst: InstalledSkill) -> dict:
    """Convert an InstalledSkill dataclass to a plain dict."""
    return asdict(inst)
