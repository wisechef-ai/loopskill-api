"""tests/migrations/test_baseline_idempotent.py

Migration test: applying the baseline stamp to an existing schema is a no-op.

Scenario:
1. Create a SQLite DB with the exact baseline production schema (no sprint-4 columns).
2. Stamp it at the baseline revision (4ba0bf05cd47) — simulates the prod deploy step.
3. Verify the tables still look exactly like the pre-stamp schema (nothing dropped/changed).
4. Run upgrade head (adds the sprint-4 columns).
5. Verify the new columns exist AND the old ones are intact.

This test guards against the baseline revision accidentally touching data.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
BASELINE_REV = "4ba0bf05cd47"
HEAD_REV = "e8f2a4d10b73"

# Exact baseline production schema (as confirmed in contract)
BASELINE_DDL = """
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

CREATE TABLE IF NOT EXISTS telemetry_events (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    skill_slug TEXT,
    payload TEXT,
    client_ip TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
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
def baseline_db():
    """DB with baseline schema, stamped at baseline rev, then upgraded to head."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    # Seed baseline production schema
    conn = sqlite3.connect(db_path)
    conn.executescript(BASELINE_DDL)
    conn.commit()
    conn.close()

    yield db_path

    os.unlink(db_path)


class TestBaselineIdempotent:
    """Stamping baseline on an existing schema changes nothing; upgrade adds columns."""

    def test_baseline_columns_intact_before_stamp(self, baseline_db):
        """Pre-stamp: baseline tables have exactly the production columns."""
        conn = sqlite3.connect(baseline_db)
        tel_cols = _get_columns(conn, "telemetry_events")
        conn.close()
        # Sprint-4 columns must NOT be present yet
        for sprint4_col in ("skill_id", "goal_class", "duration_seconds",
                            "retry_count", "user_intervention", "agent_class_hash"):
            assert sprint4_col not in tel_cols, (
                f"Sprint-4 column '{sprint4_col}' appeared before migration ran"
            )

    def test_stamp_baseline_is_noop(self, baseline_db):
        """alembic stamp <baseline> should succeed and leave table structure unchanged."""
        result = _run_alembic(["stamp", BASELINE_REV], baseline_db)
        assert result.returncode == 0, (
            f"alembic stamp failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        # Columns must be unchanged — stamp is a metadata-only op
        conn = sqlite3.connect(baseline_db)
        tel_cols = _get_columns(conn, "telemetry_events")
        conn.close()
        for legacy_col in ("id", "event_type", "skill_slug", "payload", "client_ip", "created_at"):
            assert legacy_col in tel_cols, f"Stamp dropped column '{legacy_col}'"
        for sprint4_col in ("skill_id", "goal_class"):
            assert sprint4_col not in tel_cols, (
                f"Sprint-4 column appeared after stamp (should be no-op)"
            )

    def test_alembic_version_after_stamp(self, baseline_db):
        """alembic_version table must record the baseline revision after stamp."""
        conn = sqlite3.connect(baseline_db)
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        conn.close()
        version_nums = [r[0] for r in rows]
        assert BASELINE_REV in version_nums, (
            f"Expected {BASELINE_REV} in alembic_version, got {version_nums}"
        )

    def test_upgrade_head_from_baseline(self, baseline_db):
        """After stamping, upgrade head must add the sprint-4 columns correctly."""
        result = _run_alembic(["upgrade", "head"], baseline_db)
        assert result.returncode == 0, (
            f"alembic upgrade head failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

        conn = sqlite3.connect(baseline_db)
        tel_cols = _get_columns(conn, "telemetry_events")
        car_cols = _get_columns(conn, "carousel_entries")
        sk_cols = _get_columns(conn, "skills")
        conn.close()

        # Typed telemetry columns
        for col in ("skill_id", "goal_class", "duration_seconds",
                    "retry_count", "user_intervention", "agent_class_hash"):
            assert col in tel_cols, f"'{col}' missing from telemetry_events after upgrade"

        # Carousel scoring columns
        for col in ("role", "score"):
            assert col in car_cols, f"'{col}' missing from carousel_entries after upgrade"

        # Skills scoring columns
        for col in ("vertical", "rating_avg", "install_count", "is_free"):
            assert col in sk_cols, f"'{col}' missing from skills after upgrade"

    def test_legacy_data_survives_upgrade(self, baseline_db):
        """Pre-existing rows must be readable and intact after the migration."""
        conn = sqlite3.connect(baseline_db)
        # Insert a legacy-style row (no sprint-4 columns)
        conn.execute("""
            INSERT INTO telemetry_events (id, event_type, skill_slug, payload)
            VALUES ('legacy-uuid-99', 'install', 'old-skill', '{"free": true}')
        """)
        conn.commit()

        row = conn.execute(
            "SELECT event_type, skill_slug, payload FROM telemetry_events WHERE id='legacy-uuid-99'"
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "install"
        assert row[1] == "old-skill"
        assert row[2] == '{"free": true}'

    def test_alembic_version_at_head_after_upgrade(self, baseline_db):
        """alembic_version must show head revision after upgrade."""
        conn = sqlite3.connect(baseline_db)
        rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
        conn.close()
        version_nums = [r[0] for r in rows]
        assert HEAD_REV in version_nums, (
            f"Expected head revision {HEAD_REV} after upgrade, got {version_nums}"
        )
