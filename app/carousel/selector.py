"""Carousel selector — score(skill, today) + select_top_7(db, today).

Scoring algorithm (verbatim from SPRINT4_CONTRACT.md §SCORING ALGORITHM):

  score(skill, today) =
      0.4 * log10(skill.install_count + 1)           # popularity, log-damped
    + 0.3 * recency_decay(skill.created_at, today)   # exp(-days/30)
    + 0.2 * (skill.rating_avg or 3.0) / 5.0          # quality, 0..1, default 3.0
    + 0.1 * (1.0 if skill.vertical=='agency' else 0.5) # vertical_match

Selector steps:
  1. Filter is_public=true OR (is_public IS NULL AND is_free=true)
  2. Compute scores
  3. Sort descending; tie-break by created_at DESC then slug ASC
  4. Take top 7 → assign slots 1..7
  5. Assign role per contract rules
  6. Tagline = first 80 chars of skill.description
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models import Skill

if TYPE_CHECKING:
    pass


# ── Scoring ────────────────────────────────────────────────────────────────


def _recency_decay(created_at: datetime | None, today: date) -> float:
    """exp(-days_since_created / 30). Returns 1.0 when created_at is None."""
    if created_at is None:
        return 1.0
    if isinstance(created_at, datetime):
        created_date = created_at.date()
    else:
        created_date = created_at  # already a date
    days = (today - created_date).days
    return math.exp(-days / 30.0)


def score(skill: Skill, today: date) -> float:
    """Compute carousel score for *skill* relative to *today*.

    Contract formula (verbatim):
      0.4 * log10(install_count + 1)
    + 0.3 * exp(-days_since_created / 30)
    + 0.2 * (rating_avg or 3.0) / 5.0
    + 0.1 * (1.0 if vertical=='agency' else 0.5)
    """
    install_count = skill.install_count or 0  # None → 0 (D1 optional)
    popularity = 0.4 * math.log10(install_count + 1)

    recency = 0.3 * _recency_decay(skill.created_at, today)

    rating_avg = skill.rating_avg if skill.rating_avg is not None else 3.0
    quality = 0.2 * (rating_avg / 5.0)

    vertical = getattr(skill, "vertical", None)  # D1 optional
    vertical_match = 0.1 * (1.0 if vertical == "agency" else 0.5)

    return popularity + recency + quality + vertical_match


# ── Role assignment ────────────────────────────────────────────────────────


def _is_new(skill: Skill, today: date) -> bool:
    """True if skill.created_at is within the last 30 days."""
    if skill.created_at is None:
        return False
    created_date = skill.created_at.date() if isinstance(skill.created_at, datetime) else skill.created_at
    return (today - created_date).days <= 30


def _has_same_category_older(skill: Skill, db: Session, today: date) -> bool:
    """True if another public skill with the same category AND an older created_at exists.

    F10 fix: was checking ANY same-category skill, not specifically *older* ones.
    Now requires created_at < skill.created_at so the role assignment accurately
    reflects 'this skill replaces something older'.

    NULL created_at is treated as the oldest possible (always < anything with a date).
    """
    if not skill.category:
        return False

    # Normalise skill's own created_at for comparison
    skill_created = skill.created_at
    if skill_created is None:
        # If this skill has no created_at, nothing can be older than it
        return False

    same_cat = (
        db.query(Skill)
        .filter(
            Skill.category == skill.category,
            Skill.id != skill.id,
            Skill.is_public == True,  # noqa: E712
            # F10: must be strictly older (NULL treated as oldest — always passes)
        )
        .filter(
            # NULL created_at means no date recorded → treat as oldest
            (Skill.created_at == None) | (Skill.created_at < skill_created)  # noqa: E711
        )
        .first()
    )
    return same_cat is not None


def _assign_role(slot: int, skill: Skill, db: Session, today: date) -> str:
    """Assign carousel role per contract:

    slot 1 — new-capability if created within 30d, else replaces if same-cat
              older skill exists, else experimental
    slots 2-5 — replaces for same-category overlap, new-capability otherwise
    slots 6-7 — experimental
    """
    if slot >= 6:
        return "experimental"
    if slot == 1:
        if _is_new(skill, today):
            return "new-capability"
        if _has_same_category_older(skill, db, today):
            return "replaces"
        return "experimental"
    # slots 2-5
    if _has_same_category_older(skill, db, today):
        return "replaces"
    return "new-capability"


# ── Selector ───────────────────────────────────────────────────────────────


def select_top_7(db: Session, today: date) -> list[dict]:
    """Return a list of up to 7 dicts ready to write as CarouselEntry rows.

    Each dict has:
      skill_id, skill, slot, role, tagline, score_value
    """
    # Step 1: eligibility filter (contract §Selector step 1)
    candidates = (
        db.query(Skill)
        .filter(
            (Skill.is_public == True)  # noqa: E712
            | (
                (Skill.is_public == None)  # noqa: E711
                & (Skill.is_free == True)  # noqa: E712
            )
        )
        .all()
    )

    # Step 2: compute scores
    scored: list[tuple[float, Skill]] = [(score(s, today), s) for s in candidates]

    # Step 3: sort descending; tie-break by created_at DESC, then slug ASC
    def sort_key(item: tuple[float, Skill]):
        s_val, sk = item
        # negate score for desc order; negate created_at timestamp for desc; slug asc
        created_ts = sk.created_at.timestamp() if sk.created_at is not None else 0.0
        return (-s_val, -created_ts, sk.slug)

    scored.sort(key=sort_key)

    # Step 4: take top 7
    top7 = scored[:7]

    today_dt = datetime.combine(today, datetime.min.time(), tzinfo=UTC)

    result = []
    for slot_idx, (score_val, skill) in enumerate(top7, start=1):
        slot = slot_idx  # 1-indexed

        # Step 5: assign role
        role = _assign_role(slot, skill, db, today)

        # Step 6: tagline = first 80 chars of description
        description = skill.description or ""
        tagline = description[:80] if description else skill.title[:80]

        result.append(
            {
                "skill_id": skill.id,
                "skill": skill,
                "slot": slot,
                "role": role,
                "tagline": tagline,
                "score_value": score_val,
                "featured_date": today_dt,
            }
        )

    return result
