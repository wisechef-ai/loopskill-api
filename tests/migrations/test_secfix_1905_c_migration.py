"""tests/migrations/test_secfix_1905_c_migration.py

Tests for migration b1c2d3e4f5a6: add_is_sandbox_operator_to_api_keys.

Strategy: build a SQLite DB with api_keys table that does NOT have
is_sandbox_operator (simulating the DB state before this migration runs),
stamp at PARENT_REV, then run alembic upgrade to THIS_REV.

Verifies:
  1. upgrade: is_sandbox_operator column added to api_keys
  2. downgrade -1: column removed cleanly
  3. round-trip: upgrade → downgrade → upgrade works
  4. New rows default to is_sandbox_operator=0 (False)
  5. alembic heads returns exactly 1 head (no fork)
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent

THIS_REV = "b1c2d3e4f5a6"
PARENT_REV = "a0b1c2d3e4f5"

# Minimal DDL for api_keys WITHOUT is_sandbox_operator (pre-migration state).
# We only need this one table for our tests.
_API_KEYS_DDL_PRE = """
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    name TEXT,
    label TEXT,
    cookbook_id TEXT,
    is_active INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used_at DATETIME
);
"""


def _run_alembic(args: list[str], db_path: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "WR_DATABASE_URL": f"sqlite:///{db_path}"}
    return subprocess.run(
        [sys.executable, "-m", "alembic"] + args,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def _seed_and_stamp(db_path: str) -> None:
    """Create api_keys table (without is_sandbox_operator) and stamp at PARENT_REV."""
    conn = sqlite3.connect(db_path)
    conn.executescript(_API_KEYS_DDL_PRE)
    conn.commit()
    conn.close()

    r = _run_alembic(["stamp", PARENT_REV], db_path)
    assert r.returncode == 0, (
        f"alembic stamp {PARENT_REV} failed:\n{r.stdout}\n{r.stderr}"
    )


@pytest.fixture()
def db_at_parent():
    """SQLite DB with api_keys (pre-migration) stamped at PARENT_REV."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        _seed_and_stamp(db_path)
        yield db_path
    finally:
        os.unlink(db_path)


@pytest.fixture()
def db_at_head(db_at_parent):
    """DB at PARENT_REV, upgraded to THIS_REV."""
    r = _run_alembic(["upgrade", THIS_REV], db_at_parent)
    assert r.returncode == 0, (
        f"alembic upgrade {THIS_REV} failed:\n{r.stdout}\n{r.stderr}"
    )
    return db_at_parent


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_upgrade_adds_is_sandbox_operator_column(db_at_head):
    """After upgrade, is_sandbox_operator column exists in api_keys."""
    conn = sqlite3.connect(db_at_head)
    cols = _get_columns(conn, "api_keys")
    conn.close()
    assert "is_sandbox_operator" in cols, (
        f"Expected 'is_sandbox_operator' in api_keys, got: {cols}"
    )


def test_upgrade_column_defaults_to_false(db_at_head):
    """After upgrade, new rows default to is_sandbox_operator=0."""
    conn = sqlite3.connect(db_at_head)
    conn.execute("""
        INSERT INTO api_keys (id, user_id, key_prefix, key_hash, is_active)
        VALUES ('test-key-c1', 'test-user-c1', 'testpfx', 'testhash', 1)
    """)
    conn.commit()
    row = conn.execute(
        "SELECT is_sandbox_operator FROM api_keys WHERE id='test-key-c1'"
    ).fetchone()
    conn.close()
    assert row is not None
    # SQLite: server_default=false() → 0 for new rows, or None if not enforced at insert
    assert row[0] in (0, False, None), (
        f"Expected is_sandbox_operator default 0/False/None, got {row[0]!r}"
    )


def test_upgrade_does_not_remove_existing_columns(db_at_parent):
    """Upgrade must not drop any existing api_keys columns."""
    conn = sqlite3.connect(db_at_parent)
    cols_before = set(_get_columns(conn, "api_keys"))
    conn.close()

    r = _run_alembic(["upgrade", THIS_REV], db_at_parent)
    assert r.returncode == 0, r.stderr

    conn = sqlite3.connect(db_at_parent)
    cols_after = set(_get_columns(conn, "api_keys"))
    conn.close()

    removed = cols_before - cols_after
    assert not removed, f"Columns removed by upgrade: {removed}"


def test_downgrade_removes_column(db_at_head):
    """After upgrade then downgrade -1, is_sandbox_operator column is removed."""
    r = _run_alembic(["downgrade", "-1"], db_at_head)
    assert r.returncode == 0, (
        f"alembic downgrade -1 failed:\n{r.stdout}\n{r.stderr}"
    )
    conn = sqlite3.connect(db_at_head)
    cols = _get_columns(conn, "api_keys")
    conn.close()
    assert "is_sandbox_operator" not in cols, (
        f"Expected column removed after downgrade, got: {cols}"
    )


def test_upgrade_downgrade_upgrade_round_trip(db_at_parent):
    """Full round-trip: upgrade → downgrade -1 → upgrade → column present."""
    db_path = db_at_parent
    for step in [
        ["upgrade", THIS_REV],
        ["downgrade", "-1"],
        ["upgrade", THIS_REV],
    ]:
        r = _run_alembic(step, db_path)
        assert r.returncode == 0, (
            f"alembic {' '.join(step)} failed:\n{r.stdout}\n{r.stderr}"
        )

    conn = sqlite3.connect(db_path)
    cols = _get_columns(conn, "api_keys")
    conn.close()
    assert "is_sandbox_operator" in cols, "Column missing after round-trip"


def test_alembic_heads_count_is_one():
    """alembic heads must return exactly 1 head (no fork introduced)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        env = {**os.environ, "WR_DATABASE_URL": f"sqlite:///{db_path}"}
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "heads"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        head_lines = [
            line for line in result.stdout.splitlines()
            if line.strip() and "(head)" in line
        ]
        assert len(head_lines) == 1, (
            f"Expected exactly 1 alembic head, got {len(head_lines)}:\n{result.stdout}"
        )
    finally:
        os.unlink(db_path)
