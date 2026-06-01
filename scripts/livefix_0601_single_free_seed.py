"""livefix_0601 — single-free-seed repositioning.

Flip the free-skill doctrine from 4 free skills to ONE viral free seed
(super-memory, 675 installs, evergreen-ish gateway). Demote the other three
former-free skills to Pro:

  - client-reporter         (marketing — agency money-maker, now the Pro hook)
  - chef                    (productivity)
  - obsidian-livesync-bridge

super-memory stays free. Idempotent: re-running is a no-op once applied.
Run on the host with the app venv + .env loaded:

    set -a && source .env && set +a && .venv/bin/python scripts/livefix_0601_single_free_seed.py [--dry-run]
"""

from __future__ import annotations

import sys

from app.database import SessionLocal
from app.models import Skill

# The single free seed — must remain free.
FREE_SEED = "super-memory"

# Former free skills to demote to Pro.
DEMOTE_TO_PRO = [
    "client-reporter",
    "chef",
    "obsidian-livesync-bridge",
]


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    db = SessionLocal()
    changed = []
    try:
        # Sanity: the seed must exist and be free.
        seed = db.query(Skill).filter(Skill.slug == FREE_SEED).first()
        if seed is None:
            print(f"ERROR: free seed '{FREE_SEED}' not found — aborting.")
            return 2
        if (seed.tier or "").lower() != "free":
            print(f"WARNING: seed '{FREE_SEED}' tier is '{seed.tier}', expected 'free'.")

        for slug in DEMOTE_TO_PRO:
            sk = db.query(Skill).filter(Skill.slug == slug).first()
            if sk is None:
                print(f"  SKIP {slug}: not found")
                continue
            current = (sk.tier or "").lower()
            if current == "pro":
                print(f"  OK   {slug}: already pro (idempotent no-op)")
                continue
            print(f"  {'WOULD SET' if dry_run else 'SET'} {slug}: {current!r} -> 'pro'")
            if not dry_run:
                sk.tier = "pro"
                changed.append(slug)

        if not dry_run and changed:
            db.commit()
            print(f"committed: {len(changed)} skill(s) demoted -> pro: {', '.join(changed)}")
        elif dry_run:
            print("dry-run — no writes")
        else:
            print("no changes needed")

        # Report resulting free set.
        free_now = [
            s.slug
            for s in db.query(Skill)
            .filter(Skill.is_public == True, Skill.is_archived == False, Skill.tier == "free")  # noqa: E712
            .all()
        ]
        print(f"FREE skills now ({len(free_now)}): {', '.join(sorted(free_now)) or '(none)'}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
