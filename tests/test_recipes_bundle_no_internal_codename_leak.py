"""Guard: published recipe bundles must not leak internal sprint codenames.

The recipes/ directory holds skill bundles that get tarballed and published to
the public catalog (recipes.wisechef.ai). Their SKILL.md, scripts/, references/,
and recipe.yaml are downloaded verbatim by external agents. Internal sprint
codenames (evergreen_0206, recipes_2005, "Phase D", "decision #13", host build
labels like "Phase F.9") have NO business in a public artifact — they leak our
internal process vocabulary and confuse adopters.

This shipped once: recipes-cookbook-reconcile published with
"evergreen_0206 Phase D" / "decision #13" in its client docstrings, and ten
recipe.yaml files carried a "Phase F.9" build-label comment (2026-06-04). This
test is the regression gate so it can't silently recur on the next publish.

ALLOWLIST: plan-for-goal legitimately uses `golazo` (its public alias) and
generic "Phase A/X" plan-structure vocabulary as its actual subject matter —
those are content, not leaks, so that slug is exempted from the phase/decision
patterns (but NOT from the sprint-codename patterns).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RECIPES_DIR = Path(__file__).resolve().parent.parent / "recipes"

# Internal sprint codenames — NEVER allowed in any published bundle.
SPRINT_CODENAME_RE = re.compile(
    r"\b("
    r"evergreen_0206|recipes_2005|loopclose_\d+|cookbook_share_\d+|secfix_\d+|"
    r"ahe_\d+|topshelf_\d+|fedui_\d+|quality_\d+|chef_growth_\d+|polish_\d+|"
    r"creator_\d+|pick_\d+"
    r")\b"
)

# Internal build/process labels — phase letters + numbered decisions. These are
# leaks EXCEPT where a skill's own subject matter is literally about plan phases
# (plan-for-goal). Patterns: "Phase D", "Phase F.9", "decision #13".
PROCESS_LABEL_RE = re.compile(r"\bPhase [A-Z](?:\.\d+)?\b|\bdecision #\d+\b")

# Slugs whose subject matter legitimately includes "Phase A/X" + "golazo".
PROCESS_LABEL_ALLOWLIST = {"plan-for-goal"}

# File suffixes that ship inside the published tarball.
PUBLISHED_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".toml", ".txt", ".env"}


def _published_files() -> list[Path]:
    if not RECIPES_DIR.is_dir():
        return []
    out: list[Path] = []
    for p in RECIPES_DIR.rglob("*"):
        if p.is_file() and (p.suffix in PUBLISHED_SUFFIXES or p.name == "recipes-reconcile"):
            out.append(p)
    return out


def _slug_of(path: Path) -> str:
    rel = path.relative_to(RECIPES_DIR)
    return rel.parts[0] if rel.parts else ""


@pytest.mark.parametrize("path", _published_files(), ids=lambda p: str(p.relative_to(RECIPES_DIR)))
def test_no_sprint_codename_leak(path: Path):
    """No published bundle file may contain an internal sprint codename."""
    text = path.read_text(errors="replace")
    hits = SPRINT_CODENAME_RE.findall(text)
    assert not hits, (
        f"{path.relative_to(RECIPES_DIR)} leaks internal sprint codename(s): "
        f"{sorted(set(hits))}. Scrub the docstring/comment to describe behavior, "
        f"not the sprint it was built in."
    )


@pytest.mark.parametrize("path", _published_files(), ids=lambda p: str(p.relative_to(RECIPES_DIR)))
def test_no_process_label_leak(path: Path):
    """No published bundle file may contain a 'Phase X' / 'decision #N' build
    label — except slugs whose subject matter is genuinely about plan phases."""
    if _slug_of(path) in PROCESS_LABEL_ALLOWLIST:
        pytest.skip(f"{_slug_of(path)} uses phase vocabulary as its subject matter")
    text = path.read_text(errors="replace")
    hits = PROCESS_LABEL_RE.findall(text)
    assert not hits, (
        f"{path.relative_to(RECIPES_DIR)} leaks internal process label(s): "
        f"{sorted(set(hits))}. These are build-process vocabulary, not public "
        f"skill content — rewrite to describe what the code does."
    )


def test_recipes_dir_is_present_and_nonempty():
    """Sanity: the parametrized gates above are vacuous if recipes/ is empty."""
    assert _published_files(), "recipes/ has no published bundle files — gate would be a no-op"
