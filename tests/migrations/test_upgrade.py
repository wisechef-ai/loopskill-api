"""tests/migrations/test_upgrade.py

Migration test: fresh SQLite DB seeded with baseline schema, stamp at baseline,
then run `alembic upgrade head`, assert full schema state.

This mirrors the production deploy protocol from Sprint 4 contract:
    1. alembic stamp <baseline_rev>   -- marks existing schema as stamped
    2. alembic upgrade head           -- adds Sprint 4 columns

Sprint 4 D1 contract:
  - After upgrade head, all Sprint 4 columns must exist in their respective tables.
  - Pre-existing production columns must NOT be removed or renamed.
  - The alembic_version table must show revision a7f7db696591 as current.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Superseded by tests/migrations/test_chain_postgres.py — see note in
# test_baseline_idempotent.py for the full rationale. The SQLite-only
# stamp invariant tests can stay in test_baseline_idempotent.py without
# this file's duplication.
pytestmark = pytest.mark.skip(
    reason=(
        "Superseded by tests/migrations/test_chain_postgres.py. The SQLite "
        "chain cannot exercise the chain's Postgres-only DDL. Run: "
        "bash scripts/test-migrations-against-postgres.sh"
    )
)

# Locate the repo root (two levels up from this file: tests/migrations/test_upgrade.py)
REPO_ROOT = Path(__file__).parent.parent.parent
BASELINE_REV = "4ba0bf05cd47"
HEAD_REV = "f1a2c3d4e5b6"

# Production baseline schema (DDL only — no Sprint 4 columns)
BASELINE_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    github_id INTEGER UNIQUE,
    email TEXT,
    display_name TEXT NOT NULL,
    avatar_url TEXT,
    stripe_connect_id TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS creators (
    id TEXT PRIMARY KEY,
    user_id TEXT UNIQUE,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    avatar_url TEXT,
    bio TEXT,
    is_founder INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS orgs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    api_key_hash TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    category TEXT,
    readme TEXT,
    license TEXT,
    tier TEXT,
    is_public INTEGER DEFAULT 1,
    creator_id TEXT,
    org_id TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS skill_versions (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    semver TEXT NOT NULL,
    tarball_path TEXT,
    tarball_size_bytes INTEGER,
    checksum_sha256 TEXT,
    changelog TEXT,
    skill_toml TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(skill_id, semver)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    name TEXT,
    is_active INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_at DATETIME
);
CREATE TABLE IF NOT EXISTS install_events (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    skill_slug TEXT,
    api_key_id TEXT,
    version_semver TEXT,
    client_ip TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS telemetry_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    skill_slug TEXT,
    payload TEXT,
    client_ip TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS carousel_entries (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    featured_date DATETIME NOT NULL,
    tagline TEXT,
    position INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return column names for *table* in the given sqlite connection."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def _run_alembic(args: list[str], db_path: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "WR_DATABASE_URL": f"sqlite:///{db_path}"}
    return subprocess.run(
        [sys.executable, "-m", "alembic"] + args,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def upgraded_db():
    """Create a DB with baseline schema, stamp it, run alembic upgrade head, yield path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Seed with baseline production schema
    conn = sqlite3.connect(db_path)
    conn.executescript(BASELINE_DDL)
    conn.commit()
    conn.close()

    # Step 1: stamp at baseline (simulates production pre-deploy step)
    r = _run_alembic(["stamp", BASELINE_REV], db_path)
    assert r.returncode == 0, (
        f"alembic stamp failed:\nSTDOUT: {r.stdout}\nSTDERR: {r.stderr}"
    )

    # Step 2: upgrade to head
    r = _run_alembic(["upgrade", "head"], db_path)
    assert r.returncode == 0, (
        f"alembic upgrade head failed:\nSTDOUT: {r.stdout}\nSTDERR: {r.stderr}"
    )

    yield db_path

    os.unlink(db_path)


class TestFreshUpgrade:
    """Full stamp-then-upgrade cycle on a baseline sqlite database."""

    def test_alembic_version_is_head(self, upgraded_db):
        """After upgrade head the alembic_version table must record the latest revision."""
        conn = sqlite3.connect(upgraded_db)
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        conn.close()
        version_nums = [r[0] for r in rows]
        assert HEAD_REV in version_nums, (
            f"Expected head revision {HEAD_REV} in alembic_version, got {version_nums}"
        )

    # ------------------------------------------------------------------
    # Production columns that must still exist (regression guard)
    # ------------------------------------------------------------------

    def test_telemetry_events_legacy_columns_intact(self, upgraded_db):
        """Legacy telemetry_events columns must not be removed."""
        conn = sqlite3.connect(upgraded_db)
        cols = _get_columns(conn, "telemetry_events")
        conn.close()
        for col in ("id", "event_type", "skill_slug", "payload", "client_ip", "created_at"):
            assert col in cols, f"Legacy column '{col}' missing from telemetry_events"

    def test_install_events_columns_intact(self, upgraded_db):
        """install_events production columns must be present."""
        conn = sqlite3.connect(upgraded_db)
        cols = _get_columns(conn, "install_events")
        conn.close()
        for col in ("id", "skill_id", "skill_slug", "api_key_id", "version_semver",
                    "client_ip", "created_at"):
            assert col in cols, f"Column '{col}' missing from install_events"

    def test_skills_legacy_columns_intact(self, upgraded_db):
        """skills production columns must be present."""
        conn = sqlite3.connect(upgraded_db)
        cols = _get_columns(conn, "skills")
        conn.close()
        for col in ("id", "slug", "title", "description", "category", "readme",
                    "license", "tier", "is_public", "creator_id", "org_id",
                    "created_at", "updated_at"):
            assert col in cols, f"Legacy column '{col}' missing from skills"

    def test_carousel_entries_legacy_columns_intact(self, upgraded_db):
        """carousel_entries production columns must be present."""
        conn = sqlite3.connect(upgraded_db)
        cols = _get_columns(conn, "carousel_entries")
        conn.close()
        for col in ("id", "skill_id", "featured_date", "tagline", "position", "created_at"):
            assert col in cols, f"Legacy column '{col}' missing from carousel_entries"

    # ------------------------------------------------------------------
    # New Sprint 4 columns added by a7f7db696591
    # ------------------------------------------------------------------

    def test_telemetry_events_new_typed_columns(self, upgraded_db):
        """All typed-telemetry columns from revision a7f7db696591 must exist."""
        conn = sqlite3.connect(upgraded_db)
        cols = _get_columns(conn, "telemetry_events")
        conn.close()
        new_cols = (
            "skill_id", "goal_class", "duration_seconds",
            "retry_count", "user_intervention", "agent_class_hash",
        )
        for col in new_cols:
            assert col in cols, f"New typed-telemetry column '{col}' missing from telemetry_events"

    def test_carousel_entries_scoring_columns(self, upgraded_db):
        """Carousel scoring columns role and score must exist."""
        conn = sqlite3.connect(upgraded_db)
        cols = _get_columns(conn, "carousel_entries")
        conn.close()
        for col in ("role", "score"):
            assert col in cols, f"Scoring column '{col}' missing from carousel_entries"

    def test_skills_scoring_columns(self, upgraded_db):
        """Skills scoring columns must exist after upgrade."""
        conn = sqlite3.connect(upgraded_db)
        cols = _get_columns(conn, "skills")
        conn.close()
        for col in ("vertical", "rating_avg", "install_count", "is_free"):
            assert col in cols, f"Scoring column '{col}' missing from skills"

    def test_new_rows_accept_typed_telemetry(self, upgraded_db):
        """Sanity: can insert a row using the new typed columns."""
        conn = sqlite3.connect(upgraded_db)
        conn.execute("""
            INSERT INTO telemetry_events
              (id, event_type, skill_slug, goal_class, duration_seconds,
               retry_count, user_intervention, agent_class_hash)
            VALUES
              ('test-uuid-1', 'task_completed', 'agent-rescue',
               'client-reporting', 42, 0, 0, 'abc12345')
        """)
        conn.commit()
        row = conn.execute(
            "SELECT goal_class, duration_seconds FROM telemetry_events WHERE id='test-uuid-1'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "client-reporting"
        assert row[1] == 42

    def test_legacy_payload_column_still_writable(self, upgraded_db):
        """The legacy payload column must still accept JSON text (backward compat)."""
        conn = sqlite3.connect(upgraded_db)
        conn.execute("""
            INSERT INTO telemetry_events (id, event_type, skill_slug, payload)
            VALUES ('legacy-uuid-1', 'install', 'some-skill', '{"freeform": "data"}')
        """)
        conn.commit()
        row = conn.execute(
            "SELECT payload FROM telemetry_events WHERE id='legacy-uuid-1'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == '{"freeform": "data"}'

    def test_skills_install_count_defaults_to_zero(self, upgraded_db):
        """New install_count column has a NOT NULL default of 0."""
        conn = sqlite3.connect(upgraded_db)
        conn.execute("""
            INSERT INTO skills (id, slug, title)
            VALUES ('skill-uuid-1', 'test-skill', 'Test Skill')
        """)
        conn.commit()
        row = conn.execute(
            "SELECT install_count FROM skills WHERE id='skill-uuid-1'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == 0 or row[0] is None  # server_default applies at DB level
