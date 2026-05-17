#!/usr/bin/env python3
"""polish_1805 item 4 — seed creator handle + url for known creators.

The handle/url backfill source-of-truth is a small in-tree map of known
internal creators (founder, in-house authors). External creators populate
their own handle/url at publish time via the recipify flow — that path is
unchanged; this script only fills in WiseChef's own founder rows so the
portal renders "by Adam @adamkrawczyk" instead of "by WiseChef Team".

Safety properties:
- **Idempotent.** Running twice yields identical rows.
- **Dry-run first.** Default mode prints what would change; pass --apply.
- **Per-row report.**

Usage:
    python scripts/polish_1805_seed_creator_identity.py             # dry-run
    python scripts/polish_1805_seed_creator_identity.py --apply
"""
from __future__ import annotations

import argparse
import sys


# Known internal creators. Update this when new in-house publishers join.
# External publishers set handle/url at publish time via recipify, NOT here.
KNOWN_CREATORS = {
    # match on Creator.slug (lowercase kebab-case)
    "wisechef-team": {
        "handle": "wisechef_ai",
        "url": "https://wisechef.ai",
    },
    "adam-krawczyk": {
        "handle": "adamkrawczyk",
        "url": "https://x.com/adamkrawczyk",
    },
    "tori": {
        "handle": "wisechef_ai",
        "url": "https://wisechef.ai",
    },
    "chef": {
        "handle": "wisechef_ai",
        "url": "https://wisechef.ai",
    },
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    from app.database import SessionLocal
    from app.models import Creator

    db = SessionLocal()
    try:
        changes = []
        for slug, ident in KNOWN_CREATORS.items():
            row = db.query(Creator).filter(Creator.slug == slug).first()
            if not row:
                print(f"  [{slug}] (not in DB — skipped)")
                continue
            handle_change = (row.handle or "") != ident["handle"]
            url_change = (row.url or "") != ident["url"]
            if not (handle_change or url_change):
                print(f"  [{slug}] already correct, skipped")
                continue
            changes.append((row, ident))
            print(f"  [{slug}] handle={row.handle!r}→{ident['handle']!r}  url={row.url!r}→{ident['url']!r}")

        if not changes:
            print("Nothing to change.")
            return

        if not args.apply:
            print(f"\n💡 Dry-run. {len(changes)} change(s) pending. Pass --apply.")
            return

        for row, ident in changes:
            row.handle = ident["handle"]
            row.url = ident["url"]
        db.commit()
        print(f"\n✅ Updated {len(changes)} creator row(s).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
