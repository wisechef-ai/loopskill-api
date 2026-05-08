#!/usr/bin/env bash
# scripts/restore-deleted-skill.sh — Restore a single skill from WIS-903 backup.
#
# Usage: bash scripts/restore-deleted-skill.sh <slug>
#
# Reads /srv/wiserecipes-api/backups/deleted-skills-2026-05-07/<slug>.tar.gz
# Restores: skills + skill_versions + skill_aliases + carousel_entries +
#           install_events + skill_derived_edges
#
# Idempotent: aborts cleanly if slug already exists in the catalog.
# After restore, remove the slug from retired-skills.txt and reload the API.
set -euo pipefail

SLUG="${1:?Usage: $0 <slug>}"
BACKUP_DIR="/srv/wiserecipes-api/backups/deleted-skills-2026-05-07"
TAR="$BACKUP_DIR/${SLUG}.tar.gz"

if [ ! -f "$TAR" ]; then
    echo "ERROR: No backup found for '$SLUG' at $TAR"
    echo "Available backups:"
    ls "$BACKUP_DIR"/*.tar.gz 2>/dev/null | xargs -n1 basename | sed 's/\.tar\.gz$//' | sed 's/^/  /'
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$APP_DIR"

echo "Restoring '$SLUG' from $TAR ..."

# jsonb columns in the schema — must be json-encoded for the INSERT.
JSONB_COLS_SKILLS="related_skills external_resources"
JSONB_COLS_VERSIONS="frontmatter_json metadata_json"
JSONB_COLS_GENERIC="metadata_json data_json payload_json"

.venv/bin/python3 - <<PYEOF
import json, tarfile
from app.database import engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

slug = "${SLUG}"
tar_path = "${TAR}"

# jsonb columns by table — values must be json-stringified before INSERT.
JSONB_COLS = {
    "skills": {"related_skills", "external_resources", "embedding_meta"},
    "skill_versions": {"frontmatter_json", "metadata_json", "files_json"},
    "skill_aliases": set(),
    "carousel_entries": {"metadata_json"},
    "install_events": {"metadata_json", "context_json"},
    "skill_derived_edges": {"metadata_json"},
}

with tarfile.open(tar_path, "r:gz") as tar:
    f = tar.extractfile(f"{slug}/backup.json")
    if f is None:
        raise SystemExit(f"backup.json not found in {tar_path}")
    data = json.loads(f.read())

skill = data["skill"]
versions = data.get("versions", [])
carousel = data.get("carousel_entries", [])
installs = data.get("install_events", [])
edges = data.get("derived_edges", [])
aliases = data.get("aliases", [])

def _coerce(table, row):
    """Return (cols, params, placeholders) — json-encode jsonb columns."""
    jsonb_cols = JSONB_COLS.get(table, set())
    cols = list(row.keys())
    params = {}
    placeholders = []
    for i, k in enumerate(cols):
        v = row[k]
        ph = f":p{i}"
        if k in jsonb_cols and v is not None:
            params[f"p{i}"] = json.dumps(v)
            placeholders.append(f"CAST({ph} AS jsonb)")
        elif isinstance(v, (dict, list)) and v is not None:
            # Defensive: if a list/dict slips through and column isn't in jsonb set,
            # still json-encode rather than crash with text[] mismatch.
            params[f"p{i}"] = json.dumps(v)
            placeholders.append(f"CAST({ph} AS jsonb)")
        else:
            params[f"p{i}"] = v
            placeholders.append(ph)
    return cols, params, placeholders

with engine.begin() as conn:
    # Pre-flight: skill must not exist
    existing = conn.execute(text("SELECT 1 FROM skills WHERE slug = :s"), {"s": slug}).first()
    if existing:
        raise SystemExit(f"Skill '{slug}' already exists in catalog. Aborting (use a different procedure to overwrite).")

    cols, params, ph = _coerce("skills", skill)
    conn.execute(
        text(f"INSERT INTO skills ({', '.join(cols)}) VALUES ({', '.join(ph)})"),
        params,
    )
    print(f"  Restored skill: {skill.get('title', slug)}")

    for v in versions:
        cols, params, ph = _coerce("skill_versions", v)
        conn.execute(
            text(f"INSERT INTO skill_versions ({', '.join(cols)}) VALUES ({', '.join(ph)})"),
            params,
        )
    print(f"  Restored {len(versions)} version(s)")

    for a in aliases:
        cols, params, ph = _coerce("skill_aliases", a)
        conn.execute(
            text(f"INSERT INTO skill_aliases ({', '.join(cols)}) VALUES ({', '.join(ph)})"),
            params,
        )
    print(f"  Restored {len(aliases)} alias(es)")

    for c in carousel:
        cols, params, ph = _coerce("carousel_entries", c)
        conn.execute(
            text(f"INSERT INTO carousel_entries ({', '.join(cols)}) VALUES ({', '.join(ph)})"),
            params,
        )
    print(f"  Restored {len(carousel)} carousel entries")

    for ie in installs:
        cols, params, ph = _coerce("install_events", ie)
        conn.execute(
            text(f"INSERT INTO install_events ({', '.join(cols)}) VALUES ({', '.join(ph)})"),
            params,
        )
    print(f"  Restored {len(installs)} install events")

    for e in edges:
        cols, params, ph = _coerce("skill_derived_edges", e)
        conn.execute(
            text(f"INSERT INTO skill_derived_edges ({', '.join(cols)}) VALUES ({', '.join(ph)})"),
            params,
        )
    print(f"  Restored {len(edges)} derived edges")

print("Restore complete.")
print()
print("NEXT: remove the slug from retired-skills.txt and reload the API:")
print(f"  sed -i.bak '/^{slug} /d' retired-skills.txt")
print("  systemctl --user reload wiserecipes-api  # or your reload command")
PYEOF
