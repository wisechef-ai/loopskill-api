"""scripts/quality_1705_unhappy_paths_backfill.py — Phase C-content backfill.

Per quality_1705 plan §3 Phase C step 1: every surviving SKILL.md frontmatter
declares >=3 `unhappy_paths` entries. This script injects those entries into
the YAML frontmatter of each skill's `readme` field in the DB.

Source-of-truth:
  scripts/_quality_1705_unhappy_paths.json
    {<slug>: [{"condition": "...", "recovery": "..."}, ...]}

Each readme is parsed as `---\n<yaml>\n---\n<body>`. If frontmatter exists,
we set/replace its `unhappy_paths` key. If no frontmatter, we wrap the readme
in a new frontmatter block carrying just `unhappy_paths`.

Idempotent: re-running with no diff produces zero writes.
Dry-run default; --commit to write.

CI partner: scripts/skill_quality_gate.py rejects publishes that lack
>=3 unhappy_paths entries.
"""
from __future__ import annotations

import argparse
import configparser
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
PAYLOAD_PATH = REPO_ROOT / "scripts" / "_quality_1705_unhappy_paths.json"


def get_db_url() -> str:
    url = os.environ.get("WR_DATABASE_URL")
    if url:
        return url
    cfg = configparser.ConfigParser()
    cfg.read(REPO_ROOT / "alembic.ini")
    return cfg["alembic"]["sqlalchemy.url"]


def parse_frontmatter(readme: str) -> tuple[dict[str, Any], str]:
    """Split readme into (frontmatter_dict, body). Empty dict if no FM."""
    if not readme or not readme.startswith("---"):
        return {}, readme or ""
    try:
        end = readme.index("\n---", 3)
    except ValueError:
        return {}, readme
    fm_text = readme[3:end].strip("\n")
    body = readme[end + 4 :].lstrip("\n")
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return {}, readme
    if not isinstance(data, dict):
        return {}, readme
    return data, body


def render_readme(frontmatter: dict[str, Any], body: str) -> str:
    """Re-render with stable YAML key order and unhappy_paths last."""
    # Stable ordering: preserve all keys in original order, unhappy_paths last
    ordered = {k: v for k, v in frontmatter.items() if k != "unhappy_paths"}
    if "unhappy_paths" in frontmatter:
        ordered["unhappy_paths"] = frontmatter["unhappy_paths"]
    fm_text = yaml.safe_dump(
        ordered,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )
    return f"---\n{fm_text}---\n\n{body}"


def inject_unhappy_paths(
    readme: str, entries: list[dict[str, str]]
) -> tuple[str, bool]:
    """Return (new_readme, changed)."""
    fm, body = parse_frontmatter(readme)
    existing = fm.get("unhappy_paths")
    if existing == entries:
        return readme, False
    fm["unhappy_paths"] = entries
    new = render_readme(fm, body)
    return new, True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write changes to DB. Default is dry-run.",
    )
    parser.add_argument(
        "--db-url",
        help="Override DB URL (else WR_DATABASE_URL or alembic.ini).",
    )
    parser.add_argument(
        "--payload",
        default=str(PAYLOAD_PATH),
        help=f"Path to unhappy_paths JSON (default: {PAYLOAD_PATH})",
    )
    args = parser.parse_args()

    payload_path = Path(args.payload)
    if not payload_path.exists():
        print(f"ERROR: payload file not found: {payload_path}", file=sys.stderr)
        return 1
    payload: dict[str, list[dict[str, str]]] = json.loads(payload_path.read_text())

    # Validate payload shape
    bad = []
    for slug, entries in payload.items():
        if not isinstance(entries, list) or len(entries) < 3:
            bad.append((slug, "needs >=3 entries"))
            continue
        for i, e in enumerate(entries):
            if not isinstance(e, dict) or set(e.keys()) != {"condition", "recovery"}:
                bad.append((slug, f"entry {i} bad keys"))
                break
            if not e["condition"] or not e["recovery"]:
                bad.append((slug, f"entry {i} empty value"))
                break
    if bad:
        for s, msg in bad[:20]:
            print(f"  BAD {s}: {msg}", file=sys.stderr)
        print(f"\nERROR: {len(bad)} payload entries failed validation", file=sys.stderr)
        return 1

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    db_url = args.db_url or get_db_url()
    engine = create_engine(db_url, future=True)
    Session = sessionmaker(bind=engine, future=True)

    with Session() as session:
        rows = session.execute(
            text(
                "SELECT id, slug, readme FROM skills "
                "WHERE is_public = true AND is_archived = false "
                "ORDER BY slug"
            )
        ).all()

        changed: list[str] = []
        unchanged: list[str] = []
        missing_payload: list[str] = []
        try:
            for r in rows:
                if r.slug not in payload:
                    missing_payload.append(r.slug)
                    continue
                new_readme, did_change = inject_unhappy_paths(
                    r.readme or "", payload[r.slug]
                )
                if did_change:
                    changed.append(r.slug)
                    if args.commit:
                        session.execute(
                            text("UPDATE skills SET readme = :rm WHERE id = :id"),
                            {"rm": new_readme, "id": r.id},
                        )
                else:
                    unchanged.append(r.slug)
            if args.commit:
                session.commit()
            else:
                session.rollback()
        except Exception:
            session.rollback()
            raise

    summary = {
        "total_skills": len(rows),
        "changed": len(changed),
        "unchanged": len(unchanged),
        "missing_payload": missing_payload,
        "changed_slugs": changed[:20],
    }
    print(json.dumps(summary, indent=2))
    if missing_payload:
        print(
            f"\nWARNING: {len(missing_payload)} skills in DB lack payload entries — "
            f"they will score 0 on the unhappy_paths axis: {missing_payload}",
            file=sys.stderr,
        )
    if not args.commit:
        print("\n[DRY-RUN] No changes written. Re-run with --commit to apply.")
        return 0
    print("\n[COMMITTED] readme fields updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
