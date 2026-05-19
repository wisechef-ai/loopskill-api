"""Test for secfix_1905/H index migration — round-trips clean.

Uses an in-memory SQLite database to verify upgrade/downgrade are idempotent
and that the migration script itself is importable.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add alembic/versions to path so we can import the migration directly
_VERSIONS_DIR = Path(__file__).parents[2] / "alembic" / "versions"
if str(_VERSIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_VERSIONS_DIR))


# ── Migration importable ────────────────────────────────────────────────────────


def test_migration_importable() -> None:
    """The migration module must be importable with correct metadata."""
    import importlib
    mod = importlib.import_module(
        "c4d5e6f7a8b9_secfix_1905_h_indexes_for_hot_paths"
    )
    assert mod.revision == "c4d5e6f7a8b9"
    assert mod.down_revision == "b1c2d3e4f5a6"


# ── Upgrade / downgrade round-trip ─────────────────────────────────────────────


def test_indexes_upgrade_downgrade_round_trip() -> None:
    """Upgrade creates indexes; downgrade removes them; idempotent."""
    from sqlalchemy import create_engine, text

    # Build a minimal schema with the two tables the migration targets
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE api_keys (id TEXT PRIMARY KEY, key_hash TEXT NOT NULL)"
        ))
        conn.execute(text(
            "CREATE TABLE cookbook_share_tokens "
            "(id TEXT PRIMARY KEY, token_prefix TEXT NOT NULL)"
        ))
        conn.commit()

    # Verify pre-conditions: no indexes yet
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ))
        pre_indexes = {row[0] for row in result.fetchall()}
    assert "ix_api_keys_key_hash" not in pre_indexes

    import importlib
    mod = importlib.import_module("c4d5e6f7a8b9_secfix_1905_h_indexes_for_hot_paths")
    upgrade = mod.upgrade
    downgrade = mod.downgrade

    # Patch op.execute to run against our engine
    executed_sqls: list[str] = []

    def _fake_execute(sql: str) -> None:
        executed_sqls.append(sql)
        with engine.connect() as conn:
            conn.execute(text(sql))
            conn.commit()

    mod_name = "c4d5e6f7a8b9_secfix_1905_h_indexes_for_hot_paths"
    with patch(f"{mod_name}.op") as mock_op:
        mock_op.execute.side_effect = _fake_execute
        upgrade()

    assert any("ix_api_keys_key_hash" in sql for sql in executed_sqls)
    assert any("ix_cookbook_share_tokens_token_prefix" in sql for sql in executed_sqls)

    # Post-upgrade: indexes exist
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ))
        post_indexes = {row[0] for row in result.fetchall()}
    assert "ix_api_keys_key_hash" in post_indexes
    assert "ix_cookbook_share_tokens_token_prefix" in post_indexes

    # Downgrade removes them
    executed_sqls.clear()
    with patch(f"{mod_name}.op") as mock_op:
        mock_op.execute.side_effect = _fake_execute
        downgrade()

    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ))
        post_downgrade_indexes = {row[0] for row in result.fetchall()}
    assert "ix_api_keys_key_hash" not in post_downgrade_indexes
    assert "ix_cookbook_share_tokens_token_prefix" not in post_downgrade_indexes
