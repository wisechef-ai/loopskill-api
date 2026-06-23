"""LoopSkill first-boot bootstrap.

Runs during container startup (via entrypoint.sh) to initialise the database
and seed the starter catalog. Safe to run on every container restart — all
operations are idempotent.

Usage (from /app inside the container):
    python scripts/bootstrap.py
"""

from __future__ import annotations

import os
import sys

# Ensure /app is on the path when invoked from the container entrypoint.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DEV_API_KEY = "rec_dev_wiserecipes_local_testing_key"


def _create_tables() -> None:
    from app.database import engine
    from app.models import Base

    Base.metadata.create_all(bind=engine)
    print("  [bootstrap] tables created/verified via SQLAlchemy")


def _run_seed_skills() -> None:
    from seed import seed  # type: ignore[import-untyped]

    seed()


def _run_seed_catalog() -> None:
    from app.database import SessionLocal
    from scripts.seed_starter_catalog import seed_starter_catalog

    db = SessionLocal()
    try:
        summary = seed_starter_catalog(db)
        print(f"  [bootstrap] starter catalog: {summary}")
    finally:
        db.close()


def _print_ready_banner() -> None:
    print("")
    print("=" * 62)
    print("  LoopSkill is ready!")
    print("")
    print("  API:         http://localhost:8200")
    print("  Docs:        http://localhost:8200/docs")
    print("  MCP server:  http://localhost:8200/api/mcp/http")
    print("")
    print(f"  Dev API key: {_DEV_API_KEY}")
    print("")
    print("  curl http://localhost:8200/api/healthz")
    print("  curl http://localhost:8200/api/skills/search \\")
    print('       -H "x-api-key: ' + _DEV_API_KEY + '"')
    print("=" * 62)
    print("")


def main() -> int:
    db_url = os.environ.get("WR_DATABASE_URL", "")
    print(f"[bootstrap] database: {db_url or '(default)'}")

    if "sqlite" in db_url:
        print("[bootstrap] sqlite mode — creating tables via create_all")
        _create_tables()
    else:
        print("[bootstrap] non-sqlite mode — running alembic upgrade head")
        import subprocess

        result = subprocess.run(["alembic", "upgrade", "head"])
        if result.returncode != 0:
            print("[bootstrap] ERROR: alembic upgrade failed", file=sys.stderr)
            return 1

    print("[bootstrap] seeding skills...")
    _run_seed_skills()

    print("[bootstrap] seeding starter catalog...")
    _run_seed_catalog()

    _print_ready_banner()
    return 0


if __name__ == "__main__":
    sys.exit(main())
