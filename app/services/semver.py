"""Semantic version comparison — portal_0610 B2 (§6.6).

The bug this closes: every "latest version" lookup in the codebase used
``func.max(SkillVersion.semver)`` over a STRING column. SQL string-max is
LEXICOGRAPHIC, so ``max("1.9.0", "1.10.0") == "1.9.0"`` — the moment any skill
reaches a double-digit minor/patch, sync pins fleets to the OLDER version and
the entire declare/pull + version-pin promise (L5/L7) silently selects wrong.

Fixing this purely in SQL is not portable: Postgres has
``string_to_array(semver, '.')::int[]`` ordering, but the test suite runs on
SQLite which has no such function. Since the number of versions per skill is
small (tens at most), the correct-everywhere solution is to compute the
semantic max in Python with a real version key.

``semver_key`` is tolerant: it parses the leading ``N.N.N`` numeric core,
ignores a ``v`` prefix and any ``-prerelease``/``+build`` suffix for ordering
the core, and falls back to (-1,) for an unparseable string so a malformed row
never out-sorts a real version. A prerelease sorts BEFORE its release
(``1.2.0-rc1`` < ``1.2.0``) per semver §11.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from uuid import UUID

_CORE_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?\s*$")


def semver_key(semver: str | None) -> tuple:
    """Return a sortable key for a semver string. Larger key = newer version.

    Unparseable / None → a floor key that never out-sorts a real version.
    A release sorts AFTER its prereleases (suffix presence lowers the key).
    """
    if not semver:
        return (-1, 0, 0, 0)
    m = _CORE_RE.match(semver)
    if not m:
        return (-1, 0, 0, 0)
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    # A release (no -prerelease) outranks its prereleases: trailing 1, else 0.
    has_prerelease = "-" in semver.split("+", 1)[0]
    release_rank = 0 if has_prerelease else 1
    return (major, minor, patch, release_rank)


def max_semver(semvers: Iterable[str | None]) -> str | None:
    """Return the semantically-greatest semver in *semvers*, or None if empty.

    Skips None/empty entries. Returns None when there is no valid candidate.
    """
    candidates = [s for s in semvers if s]
    if not candidates:
        return None
    return max(candidates, key=semver_key)


def latest_semver_for_skills(db, skill_ids: Iterable[UUID]) -> dict[UUID, str]:
    """Return {skill_id: latest_semver} computed SEMANTICALLY (not lexically).

    Replaces the ``func.max(SkillVersion.semver)`` GROUP BY subquery used across
    sync / reconcile / channel-select / cookbook-status. Fetches every
    (skill_id, semver) pair for the requested skills in ONE query, then folds to
    the per-skill semantic max in Python. Skills with no versions are absent
    from the result (callers treat a missing key as "no version").
    """
    from app.models import SkillVersion

    ids = list(skill_ids)
    if not ids:
        return {}
    rows = db.query(SkillVersion.skill_id, SkillVersion.semver).filter(SkillVersion.skill_id.in_(ids)).all()
    best: dict[UUID, str] = {}
    best_key: dict[UUID, tuple] = {}
    for skill_id, semver in rows:
        if not semver:
            continue
        k = semver_key(semver)
        if skill_id not in best_key or k > best_key[skill_id]:
            best_key[skill_id] = k
            best[skill_id] = semver
    return best


def latest_version_row_for_skill(db, skill_id: UUID, *, promoted_only: bool = False):
    """Return the semantically-latest SkillVersion row for *skill_id*, or None.

    portal_0610 B2 — replaces ``order_by(SkillVersion.created_at.desc())`` and
    ``func.max(semver)`` selection where a real SkillVersion object is needed.

    ``promoted_only=True`` restricts to versions with ``promoted_to_stable_at``
    set (the ``stable`` channel selector).
    """
    from app.models import SkillVersion

    q = db.query(SkillVersion).filter(SkillVersion.skill_id == skill_id)
    if promoted_only:
        q = q.filter(SkillVersion.promoted_to_stable_at.isnot(None))
    rows = q.all()
    if not rows:
        return None
    return max(rows, key=lambda v: semver_key(v.semver))
