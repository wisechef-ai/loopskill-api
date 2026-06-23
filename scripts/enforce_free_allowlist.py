"""enforce_free_allowlist — durable free-tier paywall guard.

Supersedes ``livefix_0601_single_free_seed.py`` (deleted). That script used a
HARDCODED DEMOTE-LIST, which structurally cannot catch a skill that did not
exist when the list was written. It leaked twice for exactly that reason
(2026-06-01, then again 2026-06-05 when a batch of local Hermes skills was
published with no ``tier:`` frontmatter and landed as free).

This enforcer INVERTS the logic: the free set is an ALLOWLIST. Any public,
non-archived skill that is free but NOT on the allowlist is demoted to pro.
That self-heals against any future ingest vector — manual seed, harvester,
recipify, publisher — regardless of how the skill got created.

It also reconciles the second free signal: the ``is_free`` boolean column
(used by the carousel public filter) is set True iff the slug is on the
allowlist, so the two signals can never drift apart again.

Idempotent. Run on the host with the app venv + .env loaded:

    set -a && source .env && set +a && \
        .venv/bin/python scripts/enforce_free_allowlist.py [--dry-run]

Exit codes:
    0  success (changes applied or already clean)
    2  an allowlisted seed is missing from the catalog (hard invariant break)
"""

from __future__ import annotations

import sys

from app.database import SessionLocal
from app.models import Skill

# The ONLY skills that may be free. Everything else free -> pro.
# Keep this list as the single source of truth for the free tier.
FREE_ALLOWLIST = {
    "super-memory",
    "recipes-cookbook-reconcile",
}

DEMOTE_TARGET = "pro"


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    db = SessionLocal()
    demoted: list[str] = []
    isfree_fixed: list[str] = []
    try:
        # Hard invariant: every allowlisted seed must exist and be free.
        missing = []
        for slug in sorted(FREE_ALLOWLIST):
            seed = db.query(Skill).filter(Skill.slug == slug).first()
            if seed is None:
                missing.append(slug)
                continue
            if (seed.tier or "").lower() != "free":
                print(f"  FIX  {slug}: tier {seed.tier!r} -> 'free' (allowlisted seed)")
                if not dry_run:
                    seed.tier = "free"
            if seed.is_free is not True:
                isfree_fixed.append(slug)
                if not dry_run:
                    seed.is_free = True
        if missing:
            print(f"ERROR: allowlisted seed(s) not found: {', '.join(missing)} — aborting.")
            return 2

        # Demote every OTHER free public skill to pro.
        leaked = (
            db.query(Skill)
            .filter(
                Skill.is_public == True,  # noqa: E712
                Skill.is_archived == False,  # noqa: E712
                Skill.tier == "free",
                Skill.slug.notin_(FREE_ALLOWLIST),
            )
            .all()
        )
        for sk in sorted(leaked, key=lambda s: s.slug):
            print(f"  {'WOULD DEMOTE' if dry_run else 'DEMOTE'} {sk.slug}: " f"'free' -> '{DEMOTE_TARGET}'")
            if not dry_run:
                sk.tier = DEMOTE_TARGET
                sk.is_free = False
            demoted.append(sk.slug)

        if not dry_run and (demoted or isfree_fixed):
            db.commit()
            print(
                f"committed: {len(demoted)} demoted -> {DEMOTE_TARGET}; "
                f"{len(isfree_fixed)} is_free flags reconciled"
            )
        elif dry_run:
            print("dry-run — no writes")
        else:
            print("no changes needed (already clean)")

        # Report resulting free set.
        free_now = sorted(
            s.slug
            for s in db.query(Skill)
            .filter(
                Skill.is_public == True,  # noqa: E712
                Skill.is_archived == False,  # noqa: E712
                Skill.tier == "free",
            )
            .all()
        )
        print(f"FREE skills now ({len(free_now)}): {', '.join(free_now) or '(none)'}")
        expected = sorted(FREE_ALLOWLIST)
        if free_now != expected:
            print(f"WARNING: free set {free_now} != allowlist {expected}")
            return 0  # reported, but not a hard failure on dry-run
        print("OK: free set matches allowlist exactly.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
