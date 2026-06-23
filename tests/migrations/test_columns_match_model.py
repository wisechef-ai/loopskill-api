"""tests/migrations/test_columns_match_model.py

Regression test for F1: migration must add slot, verdict, and install_event_id.
Previously the migration was missing these columns despite them being defined on
the model (CarouselEntry.slot, CarouselEntry.verdict, TelemetryEvent.install_event_id).

Runs the migration against a fresh in-memory SQLite DB and asserts all F1 columns exist.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Superseded by tests/migrations/test_chain_postgres.py
# (test_model_columns_present_after_full_upgrade does this check against
# the real Postgres dialect that production uses).
pytestmark = pytest.mark.skip(
    reason=(
        "Superseded by tests/migrations/test_chain_postgres.py. Run: "
        "bash scripts/test-migrations-against-postgres.sh"
    )
)

REPO_ROOT = Path(__file__).parent.parent.parent
BASELINE_REV = "4ba0bf05cd47"

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
def migrated_db():
    """Create baseline DB, stamp it, run upgrade head, yield path."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    conn = sqlite3.connect(db_path)
    conn.executescript(BASELINE_DDL)
    conn.commit()
    conn.close()

    r = _run_alembic(["stamp", BASELINE_REV], db_path)
    assert r.returncode == 0, f"alembic stamp failed:\n{r.stdout}\n{r.stderr}"

    r = _run_alembic(["upgrade", "head"], db_path)
    assert r.returncode == 0, f"alembic upgrade failed:\n{r.stdout}\n{r.stderr}"

    yield db_path
    os.unlink(db_path)


class TestF1ColumnsMatchModel:
    """F1 regression: migration must add slot, verdict (carousel), install_event_id (telemetry)."""

    def test_carousel_entries_has_slot(self, migrated_db):
        """F1: carousel_entries.slot column must exist after migration."""
        conn = sqlite3.connect(migrated_db)
        cols = _get_columns(conn, "carousel_entries")
        conn.close()
        assert "slot" in cols, f"carousel_entries.slot missing — F1 regression. Got: {cols}"

    def test_carousel_entries_has_verdict(self, migrated_db):
        """F1: carousel_entries.verdict column must exist after migration."""
        conn = sqlite3.connect(migrated_db)
        cols = _get_columns(conn, "carousel_entries")
        conn.close()
        assert "verdict" in cols, f"carousel_entries.verdict missing — F1 regression. Got: {cols}"

    def test_telemetry_events_has_install_event_id(self, migrated_db):
        """F1: telemetry_events.install_event_id must exist after migration."""
        conn = sqlite3.connect(migrated_db)
        cols = _get_columns(conn, "telemetry_events")
        conn.close()
        assert "install_event_id" in cols, (
            f"telemetry_events.install_event_id missing — F1 regression. Got: {cols}"
        )

    def test_all_four_f1_columns_together(self, migrated_db):
        """F1: all four model-declared columns must be in the migrated schema."""
        conn = sqlite3.connect(migrated_db)
        car_cols = _get_columns(conn, "carousel_entries")
        tel_cols = _get_columns(conn, "telemetry_events")
        conn.close()

        assert "slot" in car_cols
        assert "verdict" in car_cols
        assert "install_event_id" in tel_cols
        # install_event_id should be a string (VARCHAR-like) — test insertability
        conn2 = sqlite3.connect(migrated_db)
        conn2.execute(
            "INSERT INTO carousel_entries (id, skill_id, featured_date, slot, verdict) "
            "VALUES ('f1-test-uuid', 'some-skill-uuid', '2026-01-01', 3, 'promote')"
        )
        conn2.commit()
        row = conn2.execute(
            "SELECT slot, verdict FROM carousel_entries WHERE id='f1-test-uuid'"
        ).fetchone()
        conn2.close()
        assert row[0] == 3
        assert row[1] == "promote"
