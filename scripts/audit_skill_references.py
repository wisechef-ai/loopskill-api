#!/usr/bin/env python3
"""fdeloop_0808 Phase A — the dangling-reference publishing gate.

A published SKILL.md that says "see obviously-awesome" when no such slug is
published sends a reader to a 404. On 2026-08-08 ``hundred-million-offers`` did
exactly that, twice, and nothing in CI noticed. The portal enforces the same
invariant for its own links via ``audit-links.mjs``; the catalog had no
equivalent.

**Offline by construction.** The corpus is read from the database, the check is
pure string work. No network call, so this runs identically in CI, in a
pre-publish hook, and against a snapshot — and cannot go yellow because the
site is slow. A gate that flaps gets muted, and a muted gate is worse than none.

Usage::

    python scripts/audit_skill_references.py            # exit 1 on any dangle
    python scripts/audit_skill_references.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal  # noqa: E402
from app.models import Skill  # noqa: E402
from app.services.skill_refs import (  # noqa: E402
    find_dangling_references,
    format_dangling_report,
)


def load_published_corpus(db) -> tuple[dict[str, str], set[str]]:
    """Return ({slug: readme}, {published slugs}) for the reader-visible catalog.

    "Published" is the same predicate the detail route uses — public AND not
    archived. Anything else is not reachable by a visitor, so a reference to it
    IS dangling from the reader's point of view.
    """
    rows = (
        db.query(Skill.slug, Skill.readme)
        .filter(Skill.is_public.is_(True), Skill.is_archived.is_(False))
        .all()
    )
    readmes = {slug: (readme or "") for slug, readme in rows}
    return readmes, set(readmes)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a report")
    args = ap.parse_args(argv)

    db = SessionLocal()
    try:
        readmes, published = load_published_corpus(db)
    finally:
        db.close()

    dangling = find_dangling_references(readmes, published)

    if args.json:
        print(
            json.dumps(
                {
                    "checked": len(readmes),
                    "dangling_count": sum(len(v) for v in dangling.values()),
                    "dangling": {k: sorted(v) for k, v in sorted(dangling.items())},
                },
                indent=2,
            )
        )
    else:
        print(f"Checked {len(readmes)} published skill(s).")
        print(format_dangling_report(dangling))

    return 1 if dangling else 0


if __name__ == "__main__":
    raise SystemExit(main())
