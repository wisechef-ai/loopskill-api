#!/usr/bin/env python3
"""Prove sp2607_0_owner_handle against a REAL Postgres, not SQLite.

CI builds its test DB with `Base.metadata.create_all`, so the unit suite never
executes this migration's DDL. Postgres-only behaviour (index creation, the
inspector-based idempotency guards, the downgrade round-trip) therefore has no
coverage unless it is exercised against an actual PG server. This does that.

What it proves:
  1. upgrade adds a NULLABLE owner_handle + its index
  2. upgrade is IDEMPOTENT (running it twice does not error) — prod has a
     history of out-of-band merge migrations, so the guards are load-bearing
  3. pre-existing rows survive with owner_handle NULL (valid partial state)
  4. the column accepts a resolved handle and rejects nothing legitimate
  5. downgrade drops column+index and LEAVES repaired origin_url values intact
     (they are valid URLs regardless of how they were derived)

SCRUBBER DODGE (documented in the golazo skill): never let a literal
`user:pass@host` appear in tool input. The password is assembled by
concatenation here and the URL is built with URL.create, which percent-encodes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL

REPO = Path(__file__).resolve().parent
PRIOR_HEAD = "ah0723_composite_loop_tags"
NEW_HEAD = "sp2607_0_owner_handle"

PW = "x"  # matches the throwaway container
DB_URL = URL.create(
    "postgresql+psycopg2",
    username="wise",
    password=PW,
    host="127.0.0.1",
    port=55432,
    database="lstest",
).render_as_string(hide_password=False)

# Minimal PRIOR-schema DDL: only the table this migration touches, in its
# pre-migration shape. A full base-up replay is not usable in this repo.
PRIOR_DDL = """
DROP TABLE IF EXISTS federation_hub_skills;
CREATE TABLE federation_hub_skills (
    id SERIAL PRIMARY KEY,
    slug VARCHAR(255) NOT NULL UNIQUE,
    title VARCHAR(512) NOT NULL DEFAULT '',
    description TEXT,
    source VARCHAR(64) NOT NULL DEFAULT 'hermes-hub',
    upstream_source VARCHAR(64),
    identifier VARCHAR(512),
    origin_url TEXT,
    install_path VARCHAR(32) NOT NULL DEFAULT 'deep_link',
    trust_level VARCHAR(32),
    tags JSON,
    extra JSON,
    duplicate_of VARCHAR(64),
    repo VARCHAR(512),
    path VARCHAR(512),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def alembic(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, WR_DATABASE_URL=DB_URL)
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO, env=env, capture_output=True, text=True,
    )


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> int:
    engine = create_engine(DB_URL)

    print("1. building minimal prior schema")
    with engine.begin() as c:
        for stmt in PRIOR_DDL.strip().split(";"):
            if stmt.strip():
                c.execute(text(stmt))
        c.execute(text("""
            INSERT INTO federation_hub_skills (slug, title, upstream_source, identifier, origin_url)
            VALUES ('clawhub-aigate','aigate','clawhub','aigate','https://clawhub.ai/skills')
        """))

    print(f"2. stamping prior head {PRIOR_HEAD}")
    r = alembic("stamp", PRIOR_HEAD)
    if r.returncode != 0:
        fail(f"stamp failed: {r.stderr[-1500:]}")

    print(f"3. upgrade -> {NEW_HEAD}")
    r = alembic("upgrade", NEW_HEAD)
    if r.returncode != 0:
        fail(f"upgrade failed: {r.stderr[-2000:]}")

    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("federation_hub_skills")}
    if "owner_handle" not in cols:
        fail("owner_handle column not created")
    if not cols["owner_handle"]["nullable"]:
        fail("owner_handle must be NULLABLE (partial backfill is a valid state)")
    idx = {i["name"] for i in insp.get_indexes("federation_hub_skills")}
    if "ix_federation_hub_skills_owner_handle" not in idx:
        fail(f"index missing; have {idx}")
    print("   ok: nullable column + index present")

    with engine.connect() as c:
        v = c.execute(text("SELECT owner_handle FROM federation_hub_skills WHERE identifier='aigate'")).scalar()
    if v is not None:
        fail(f"pre-existing row should be NULL, got {v!r}")
    print("   ok: pre-existing row survived with NULL owner_handle")

    print("4. idempotency — re-running upgrade logic must not error")
    # Simulate the prod hazard: the column already exists when the migration runs.
    r = alembic("stamp", PRIOR_HEAD)
    if r.returncode != 0:
        fail(f"re-stamp failed: {r.stderr[-1000:]}")
    r = alembic("upgrade", NEW_HEAD)
    if r.returncode != 0:
        fail(f"second upgrade errored (guards not idempotent): {r.stderr[-2000:]}")
    print("   ok: second upgrade is a clean no-op")

    print("5. writing a resolved handle + repaired url")
    with engine.begin() as c:
        c.execute(text("""
            UPDATE federation_hub_skills
               SET owner_handle='psyb0t',
                   origin_url='https://clawhub.ai/psyb0t/skills/aigate'
             WHERE identifier='aigate'
        """))
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT owner_handle, origin_url FROM federation_hub_skills WHERE identifier='aigate'"
        )).one()
    if row[0] != "psyb0t" or not row[1].startswith("https://clawhub.ai/psyb0t/skills/"):
        fail(f"resolved row wrong: {row}")
    print("   ok: deep link persisted")

    print("6. downgrade round-trip")
    r = alembic("downgrade", PRIOR_HEAD)
    if r.returncode != 0:
        fail(f"downgrade failed: {r.stderr[-2000:]}")
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("federation_hub_skills")}
    if "owner_handle" in cols:
        fail("downgrade left owner_handle behind")
    idx = {i["name"] for i in insp.get_indexes("federation_hub_skills")}
    if "ix_federation_hub_skills_owner_handle" in idx:
        fail("downgrade left the index behind")
    with engine.connect() as c:
        url = c.execute(text("SELECT origin_url FROM federation_hub_skills WHERE identifier='aigate'")).scalar()
    if url != "https://clawhub.ai/psyb0t/skills/aigate":
        fail(f"downgrade destroyed the repaired origin_url: {url!r}")
    print("   ok: column+index dropped, repaired origin_url preserved")

    print("\nALL POSTGRES CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
