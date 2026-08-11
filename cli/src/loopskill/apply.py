"""apply — converge a local skill directory to a pulled bundle. Local only.

Defaults to dry-run. Writing requires an explicit --write flag, and every
plan (dry-run or real) prints exactly what it would do / did before any
byte touches disk. Idempotent by construction: an unchanged skill (same
sha256 already on disk) is reported as "up to date" and never rewritten, so
running apply twice in a row produces zero writes on the second run.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from loopskill.pull import PulledSkill


@dataclass(frozen=True)
class ApplyAction:
    """One planned filesystem action for a single skill."""

    name: str
    kind: str  # "create" | "update" | "up-to-date" | "skip-locked"
    path: Path


def plan_apply(skills: list[PulledSkill], dest: Path) -> list[ApplyAction]:
    """Compute what apply WOULD do, without touching disk.

    - locked (paid-tier) entries always plan as "skip-locked" — never written.
    - a target whose SKILL.md already has the identical sha256 plans as
      "up-to-date" — this is the idempotency guarantee: a second `apply` over
      the same bundle plans zero create/update actions.
    """
    actions: list[ApplyAction] = []
    for skill in skills:
        target_dir = dest / skill.name
        target_file = target_dir / "SKILL.md"
        if skill.locked:
            actions.append(ApplyAction(name=skill.name, kind="skip-locked", path=target_file))
            continue
        if target_file.is_file():
            existing = target_file.read_bytes()
            if hashlib.sha256(existing).hexdigest() == hashlib.sha256(skill.content).hexdigest():
                actions.append(ApplyAction(name=skill.name, kind="up-to-date", path=target_file))
                continue
            actions.append(ApplyAction(name=skill.name, kind="update", path=target_file))
        else:
            actions.append(ApplyAction(name=skill.name, kind="create", path=target_file))
    return actions


def execute_apply(skills: list[PulledSkill], dest: Path, actions: list[ApplyAction]) -> None:
    """Actually write files for actions planned as create/update. Local disk only."""
    by_name = {s.name: s for s in skills}
    for action in actions:
        if action.kind not in {"create", "update"}:
            continue
        skill = by_name[action.name]
        action.path.parent.mkdir(parents=True, exist_ok=True)
        action.path.write_bytes(skill.content)


def format_plan(actions: list[ApplyAction], *, dry_run: bool) -> str:
    """Human-readable plan/result report — printed before AND instead of writing."""
    verb = "would" if dry_run else "did"
    lines = [f"loopskill apply ({'dry-run' if dry_run else 'write'}):"]
    counts: dict[str, int] = {}
    for a in actions:
        counts[a.kind] = counts.get(a.kind, 0) + 1
        if a.kind == "create":
            lines.append(f"  {verb} create   {a.path}")
        elif a.kind == "update":
            lines.append(f"  {verb} update   {a.path}")
        elif a.kind == "up-to-date":
            lines.append(f"  up-to-date       {a.path}")
        elif a.kind == "skip-locked":
            lines.append(f"  skip (locked)    {a.name} — requires a paid LoopSkill tier")
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    lines.append(f"\n{len(actions)} skill(s) planned: {summary or 'none'}")
    if dry_run and (counts.get("create") or counts.get("update")):
        lines.append("Re-run with --write to apply.")
    return "\n".join(lines)
