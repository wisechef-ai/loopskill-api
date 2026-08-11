"""Discover skills already installed on disk. Zero network, by construction.

This module must never import anything that can touch the network (socket,
urllib, http.client, requests, httpx, ...) — see tests/test_offline_guard.py,
which statically audits this file's imports and also proves it at runtime by
breaking socket.socket and confirming a full scan still succeeds.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from loopskill.clients import ClientRoot, known_clients

#: SKILL.md frontmatter `description:` line, single- or double-quoted or bare.
_DESC_RE = re.compile(r"(?im)^\s*description\s*:\s*(.+?)\s*$")
_NAME_RE = re.compile(r"(?im)^\s*name\s*:\s*(.+?)\s*$")

#: Top-level directory names under a client root that are internal state, not
#: portable skill content (archives, hub caches, point-in-time snapshots).
#: Excluded deliberately and documented here rather than silently — a skill
#: someone actually wants portable should not live under a dot-directory.
_EXCLUDED_TOP_LEVEL_PREFIXES = (".",)


@dataclass(frozen=True)
class SkillRecord:
    """One discovered skill: its portable identity + content checksum."""

    skill_id: str  # relative posix path from the client root, e.g. "devops/foo"
    sha256: str  # sha256 of the raw SKILL.md bytes
    size_bytes: int
    description: str | None
    name: str | None


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in {'"', "'"}:
        return v[1:-1]
    return v


def _extract_field(text: str, pattern: re.Pattern[str]) -> str | None:
    m = pattern.search(text)
    if not m:
        return None
    return _strip_quotes(m.group(1)) or None


def scan_client(root: Path) -> list[SkillRecord]:
    """Scan one client root for SKILL.md files. Returns [] if root is absent.

    An absent directory is NOT an error (a client that was never installed
    is a normal state) — callers get an empty list, never an exception.
    """
    if not root.is_dir():
        return []

    records: list[SkillRecord] = []
    for skill_md in sorted(root.rglob("SKILL.md")):
        try:
            rel = skill_md.relative_to(root)
        except ValueError:
            continue
        parts = rel.parts
        if parts and parts[0].startswith(_EXCLUDED_TOP_LEVEL_PREFIXES):
            continue
        # The skill id is the SKILL.md's parent directory, relative to root,
        # in posix form — stable across OSes and diffable between machines.
        skill_id = "/".join(parts[:-1]) if len(parts) > 1 else skill_md.stem
        try:
            raw = skill_md.read_bytes()
        # Rationale: a single unreadable/corrupt SKILL.md must not abort the
        # whole scan — skip it and keep going, matching "absent client" being
        # a non-error, not an exception path.
        except OSError:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8", errors="replace")
        records.append(
            SkillRecord(
                skill_id=skill_id,
                sha256=digest,
                size_bytes=len(raw),
                description=_extract_field(text, _DESC_RE),
                name=_extract_field(text, _NAME_RE),
            )
        )
    records.sort(key=lambda r: r.skill_id)
    return records


@dataclass(frozen=True)
class ClientScan:
    """One client's scan result, paired with its root metadata."""

    client: ClientRoot
    skills: list[SkillRecord]


def scan_all(home: Path | None = None) -> list[ClientScan]:
    """Scan every known client. Absent clients come back with skills=[]."""
    return [ClientScan(client=c, skills=scan_client(c.path)) for c in known_clients(home)]
