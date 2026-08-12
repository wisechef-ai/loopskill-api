"""Publish-time reference gate: does this SKILL.md point readers at 404s?

Extracted from ``publisher_routes.py`` (2026-08-12) rather than waived past the
600-line module ceiling. The gate is ~70 lines of policy that has nothing to do
with tarball validation, signature verification, or version-row bookkeeping —
it reads better here and is testable without constructing a publish request.

WHY THE GATE EXISTS
───────────────────
``app/services/skill_refs.py`` shipped in #208 with a docstring calling itself
"the dangling-reference publishing gate". It was imported by six unit tests and
one manual script that is in no cron and no CI workflow — and by **nothing in
the request path**. The service was correct, well-tested, and had never once
prevented a 404.

Measured on the live catalog 2026-08-12: 7 dangling references across 5
published skills, reproduced independently by two implementations (this service
via ``scripts/audit_skill_references.py``, and the fleet's
``fdeloop0808-frontdoor`` standing predicate) agreeing exactly.

WARN, NOT BLOCK — the design decision
─────────────────────────────────────
* The existing dangles are pre-existing. A blocking gate would make those 5
  skills unpublishable until unrelated copy is rewritten — it would fail the
  next innocent republish of ``clean-code`` for a defect that shipped weeks ago.
* A forward reference is legitimately valid-later: publishing a pack A→B in two
  calls means A dangles for the seconds between them.
* By the time this runs the tarball is stored and the version row exists, so
  raising would leave a half-published skill.

So it records — in the response ``warnings`` the publisher already reads, and in
the log. The standing predicate is what makes it enforcing: same split as a lint
pre-flight plus a standing goal.

Flip to blocking only when the live count is 0 AND publish-a-pack has an atomic
multi-skill path. Until both hold, blocking trades a real 404 for a worse
failure mode.
"""

from __future__ import annotations

import logging

from app.models import Skill
from app.services.skill_refs import find_dangling_references

logger = logging.getLogger(__name__)

WARNING_SOURCE = "dangling_refs"
WARNING_CODE = "unresolved_skill_reference"


def published_slug_set(db, *, include: str | None = None) -> set[str]:
    """Every slug a reader can actually reach.

    ``include`` is added unconditionally — the skill currently being published
    is reachable by definition, even though its row may not yet satisfy the
    filter inside this transaction.
    """
    slugs = {
        row[0]
        for row in db.query(Skill.slug).filter(Skill.is_public.is_(True), Skill.is_archived.is_(False)).all()
    }
    if include:
        slugs.add(include)
    return slugs


def dangling_reference_warning(slug: str, readme: str | None, db) -> dict | None:
    """Return a publish warning if ``readme`` points readers at unpublished skills.

    Returns ``None`` when the copy is clean, so an empty result IS the pass
    condition and the caller needs no separate boolean.

    Never raises: an unavailable check is reported, not fatal. The publish is
    the product; the gate is advisory.
    """
    if not readme:
        return None

    try:
        published = published_slug_set(db, include=slug)
        dangling = sorted(find_dangling_references({slug: readme}, published).get(slug, set()))
    except Exception as exc:  # noqa: BLE001 — a broken gate must not break publishing
        logger.warning("publish: dangling-reference check failed for %s: %s", slug, exc)
        return None

    if not dangling:
        return None

    logger.warning(
        "publish: %s references %d unpublished skill(s) — readers hit a 404: %s",
        slug,
        len(dangling),
        ", ".join(dangling),
    )

    # Same shape as the security/quality findings already carried in the
    # response, so existing publisher clients render it with no change. The
    # publisher is the one person who can fix the copy and they are holding the
    # tarball right now — a log line they never read is not a notification.
    return {
        "severity": "warn",
        "source": WARNING_SOURCE,
        "code": WARNING_CODE,
        "message": (
            f"SKILL.md points readers at {len(dangling)} skill(s) that are not "
            f"published — they will hit a 404: {', '.join(dangling)}. "
            "Publish them, or reword the reference."
        ),
        "refs": dangling,
    }
