"""Drift, computed between two lockfiles. This is the demo.

Two machines, one command: ``loopskill diff machine-a.lock.json`` (against a
live scan of the current machine) or ``loopskill diff a.lock.json
b.lock.json`` (two saved snapshots, neither of which has to be this box).
Zero network either way — a lockfile is just JSON on disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SkillDiff:
    """Drift for one client between lockfile A and lockfile B."""

    client_id: str
    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)  # same id, different sha256
    unchanged_count: int = 0

    @property
    def has_drift(self) -> bool:
        return bool(self.only_in_a or self.only_in_b or self.changed)


def _skills_by_id(client_payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not client_payload:
        return {}
    return {s["id"]: s for s in client_payload.get("skills", [])}


def diff_lockfiles(lock_a: dict[str, Any], lock_b: dict[str, Any]) -> list[SkillDiff]:
    """Compute per-client drift between two lockfile dicts.

    Compares the UNION of client ids seen in either file — a client present
    in one lockfile and absent from the other (e.g. one machine has Cursor,
    the other doesn't) shows every skill as one-sided drift rather than
    being silently skipped.
    """
    clients_a = lock_a.get("clients", {})
    clients_b = lock_b.get("clients", {})
    all_client_ids = sorted(set(clients_a) | set(clients_b))

    results: list[SkillDiff] = []
    for client_id in all_client_ids:
        a_skills = _skills_by_id(clients_a.get(client_id))
        b_skills = _skills_by_id(clients_b.get(client_id))

        ids_a, ids_b = set(a_skills), set(b_skills)
        only_a = sorted(ids_a - ids_b)
        only_b = sorted(ids_b - ids_a)
        common = ids_a & ids_b
        changed = sorted(sid for sid in common if a_skills[sid]["sha256"] != b_skills[sid]["sha256"])
        unchanged = len(common) - len(changed)

        results.append(
            SkillDiff(
                client_id=client_id,
                only_in_a=only_a,
                only_in_b=only_b,
                changed=changed,
                unchanged_count=unchanged,
            )
        )
    return results


def format_diff_report(diffs: list[SkillDiff], *, label_a: str, label_b: str) -> str:
    """Human-readable drift report, the shape the README's 30-second demo shows."""
    lines: list[str] = [f"loopskill diff: {label_a}  vs  {label_b}", ""]
    any_drift = False
    for d in diffs:
        if not d.has_drift:
            lines.append(f"[{d.client_id}] in sync ({d.unchanged_count} skill(s))")
            continue
        any_drift = True
        lines.append(f"[{d.client_id}] DRIFT DETECTED")
        for sid in d.only_in_a:
            lines.append(f"  - only in {label_a}: {sid}")
        for sid in d.only_in_b:
            lines.append(f"  + only in {label_b}: {sid}")
        for sid in d.changed:
            lines.append(f"  ~ changed:          {sid}")
        if d.unchanged_count:
            lines.append(f"  ({d.unchanged_count} unchanged)")
    lines.append("")
    lines.append("DRIFT FOUND" if any_drift else "No drift — machines match.")
    return "\n".join(lines)
