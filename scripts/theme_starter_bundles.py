#!/usr/bin/env python3
"""atomic-habits 2026-07-31 rank-8 REVENUE/CATALOG.

The two STARTER_BUNDLES seeded by seed_starter_catalog.py
(dev-agent-essentials, research-and-report) are the FIRST cards
GET /api/bundles/discover?sort=installs renders (they're also the first
thing /browse?type=bundles shows — browse.astro:481 hits this exact
endpoint). They shipped with theme=None and is_editorial=False — unbranded,
unclaimed playlist cards at the very top of the storefront funnel.

This is a thin, idempotent, slug-keyed UPDATE (no new rows, no schema
change): sets theme_json + is_editorial=True (matches the "Spotify
editorial playlist" semantics already on the model, see models.py:947) and
rewrites descriptions as outcome-first playlist copy per the brief's
first_step ("Ship a PR without supervision" beats "The core skill set
for...").

Usage:
    python scripts/theme_starter_bundles.py            # apply
    python scripts/theme_starter_bundles.py --dry-run   # preview only
"""

from __future__ import annotations

import sys

# Slug -> (theme_json, is_editorial, new description). Outcome-first copy;
# theme_json mirrors the shape already read by bundle_deployment_routes.py
# (`"theme": cb.theme_json`) and the public discover card (bundle_routes.py).
BUNDLE_UPDATES: dict[str, dict] = {
    "dev-agent-essentials": {
        "theme_json": {
            "label": "Ship a PR without supervision",
            "accent": "#22c55e",
            "icon": "code-review",
        },
        "is_editorial": True,
        "description": (
            "Ship a PR without supervision. Code review, CI fix, and PR draft "
            "automation, wired together — install once and your agent reviews, "
            "fixes, and drafts the pull request end to end."
        ),
    },
    "research-and-report": {
        "theme_json": {
            "label": "A client-ready report, on a schedule",
            "accent": "#0ea5e9",
            "icon": "research",
        },
        "is_editorial": True,
        "description": (
            "A client-ready report, on a schedule. Search the web, summarise what "
            "matters, and publish a formatted deliverable your agent hands off "
            "without you touching a doc."
        ),
    },
}


def apply(dry_run: bool = False) -> int:
    from app.database import SessionLocal
    from app.models import Bundle

    db = SessionLocal()
    updated, missing = 0, []
    try:
        for slug, spec in BUNDLE_UPDATES.items():
            cb = db.query(Bundle).filter(Bundle.slug == slug).first()
            if cb is None:
                missing.append(slug)
                continue
            if cb.is_base:
                print(f"REFUSING to mutate is_base bundle for slug={slug}", file=sys.stderr)
                continue
            cb.theme_json = spec["theme_json"]
            cb.is_editorial = spec["is_editorial"]
            cb.description = spec["description"]
            updated += 1

        if dry_run:
            print(f"[dry-run] would update={updated} missing={missing}")
            db.rollback()
            return 0

        db.commit()
        print(f"theme_starter_bundles complete: updated={updated}")
        if missing:
            print(f"WARNING — missing bundle slugs (skipped): {missing}")

        # Verification read-back.
        for slug in BUNDLE_UPDATES:
            cb = db.query(Bundle).filter(Bundle.slug == slug).first()
            if cb is not None:
                print(
                    f"verify: slug={cb.slug} is_editorial={cb.is_editorial} "
                    f"theme={cb.theme_json.get('label') if cb.theme_json else None}"
                )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(apply(dry_run="--dry-run" in sys.argv))
