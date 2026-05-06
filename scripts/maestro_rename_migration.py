"""maestro_rename_migration — atomic chef→maestro rename (idempotent).

Run:
    .venv/bin/python scripts/maestro_rename_migration.py [--dry-run]

What it does (every step is a no-op if already applied):
  1. UPDATE skills SET slug='maestro', title='Maestro' WHERE slug='chef'
  2. INSERT into skill_aliases (chef → maestro, 90-day TTL)
  3. Rename recipes/chef/ directory to recipes/maestro/ if present
  4. Write a JSON audit log to migrations/maestro_rename_<timestamp>.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make repo root importable when invoked from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sqlalchemy as sa  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Skill, SkillAlias  # noqa: E402


OLD_SLUG = "chef"
NEW_SLUG = "maestro"
NEW_TITLE = "Maestro"
ALIAS_TTL_DAYS = 90


def _audit(changes: list[dict], dry_run: bool) -> Path | None:
    if dry_run:
        return None
    audit_dir = ROOT / "migrations"
    audit_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = audit_dir / f"maestro_rename_{ts}.json"
    path.write_text(
        json.dumps(
            {
                "ran_at": datetime.now(timezone.utc).isoformat(),
                "old_slug": OLD_SLUG,
                "new_slug": NEW_SLUG,
                "changes": changes,
            },
            indent=2,
            default=str,
        )
    )
    return path


def _rename_skill_row(session, changes: list[dict], dry_run: bool) -> None:
    skill_old = session.query(Skill).filter(Skill.slug == OLD_SLUG).one_or_none()
    skill_new = session.query(Skill).filter(Skill.slug == NEW_SLUG).one_or_none()

    if skill_old is None and skill_new is not None:
        changes.append({"step": "rename_skill_row", "status": "already_renamed"})
        return
    if skill_old is None and skill_new is None:
        changes.append({"step": "rename_skill_row", "status": "no_chef_row"})
        return
    if skill_old is not None and skill_new is not None:
        # Two rows present — refuse to merge silently. Operator must decide.
        changes.append(
            {
                "step": "rename_skill_row",
                "status": "conflict_both_exist",
                "chef_id": str(skill_old.id),
                "maestro_id": str(skill_new.id),
            }
        )
        return

    # skill_old is not None, skill_new is None — do the rename.
    old_title = skill_old.title
    if dry_run:
        changes.append(
            {
                "step": "rename_skill_row",
                "status": "would_rename",
                "old_slug": OLD_SLUG,
                "new_slug": NEW_SLUG,
                "old_title": old_title,
                "new_title": NEW_TITLE,
            }
        )
        return

    skill_old.slug = NEW_SLUG
    if old_title and old_title.strip().lower() in {"chef", "the chef"}:
        skill_old.title = NEW_TITLE
    session.flush()
    changes.append(
        {
            "step": "rename_skill_row",
            "status": "renamed",
            "skill_id": str(skill_old.id),
            "old_title": old_title,
            "new_title": skill_old.title,
        }
    )


def _ensure_alias(session, changes: list[dict], dry_run: bool) -> None:
    existing = (
        session.query(SkillAlias).filter(SkillAlias.old_slug == OLD_SLUG).one_or_none()
    )
    if existing is not None:
        changes.append(
            {
                "step": "ensure_alias",
                "status": "already_present",
                "expires_at": existing.expires_at,
            }
        )
        return
    if dry_run:
        changes.append({"step": "ensure_alias", "status": "would_insert"})
        return
    expires = datetime.now(timezone.utc) + timedelta(days=ALIAS_TTL_DAYS)
    session.add(SkillAlias(old_slug=OLD_SLUG, new_slug=NEW_SLUG, expires_at=expires))
    session.flush()
    changes.append({"step": "ensure_alias", "status": "inserted", "expires_at": expires})


def _rename_recipe_dir(changes: list[dict], dry_run: bool) -> None:
    src = ROOT / "recipes" / OLD_SLUG
    dst = ROOT / "recipes" / NEW_SLUG
    if not src.exists():
        changes.append({"step": "rename_recipe_dir", "status": "no_source_dir"})
        return
    if dst.exists():
        changes.append(
            {"step": "rename_recipe_dir", "status": "destination_exists_skipping"}
        )
        return
    if dry_run:
        changes.append(
            {"step": "rename_recipe_dir", "status": "would_rename", "from": str(src), "to": str(dst)}
        )
        return
    shutil.move(str(src), str(dst))
    changes.append({"step": "rename_recipe_dir", "status": "renamed", "to": str(dst)})


def main() -> int:
    parser = argparse.ArgumentParser(description="chef→maestro idempotent rename")
    parser.add_argument("--dry-run", action="store_true", help="print actions, change nothing")
    args = parser.parse_args()

    changes: list[dict] = []
    session = SessionLocal()
    try:
        _rename_skill_row(session, changes, args.dry_run)
        _ensure_alias(session, changes, args.dry_run)
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    except sa.exc.SQLAlchemyError as e:
        session.rollback()
        changes.append({"step": "db_error", "error": str(e)})
        raise
    finally:
        session.close()

    _rename_recipe_dir(changes, args.dry_run)

    audit_path = _audit(changes, args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, "audit": str(audit_path) if audit_path else None, "changes": changes}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
