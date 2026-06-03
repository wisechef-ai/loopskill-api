"""Channel-aware version selection — evergreen_0206 Phase C.

A fleet subscribes a cookbook on one of three channels. The channel decides
WHICH version of each skill that subscription should converge to:

  canary → the latest published semver (bleeding edge, any version)
  stable → the latest semver that PASSED the health/eval gate
           (SkillVersion.promoted_to_stable_at IS NOT NULL) — Phase E writes it
  frozen → no version movement at all (the subscription holds whatever it has)

This is the primitive that makes channels real (recon: they were inert labels).
Phase E's promotion engine flips promoted_to_stable_at; this module READS it.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import SkillVersion

CANARY = "canary"
STABLE = "stable"
FROZEN = "frozen"
VALID_CHANNELS = frozenset({CANARY, STABLE, FROZEN})


def latest_version_for_channel(db: Session, skill_id: UUID, channel: str) -> str | None:
    """Return the target semver for *skill_id* on *channel*, or None.

    canary → max(semver) over all versions.
    stable → max(semver) over versions with promoted_to_stable_at NOT NULL.
    frozen → None (caller holds its current pin — no movement).

    None on canary/stable means the skill has no eligible version (e.g. stable
    requested but nothing promoted yet) — the caller should not advance.
    """
    if channel == FROZEN:
        # Frozen never moves; the caller keeps its existing pin.
        return None

    q = db.query(func.max(SkillVersion.semver)).filter(SkillVersion.skill_id == skill_id)
    if channel == STABLE:
        q = q.filter(SkillVersion.promoted_to_stable_at.isnot(None))
    # canary: no extra filter — latest of anything.

    result = q.scalar()
    return result


def is_frozen(channel: str) -> bool:
    return channel == FROZEN
