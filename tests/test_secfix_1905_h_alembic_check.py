"""Issue #21 — Base.metadata.create_all removed; alembic check at boot.

Tests:
1. check_alembic_heads() is a no-op in sqlite envs (skips the check).
2. With a pg-style URL and mismatched heads → raises RuntimeError.
3. grep check: Base.metadata.create_all is absent from app/main.py.
4. create_app() calls check_alembic_heads (mocked) instead of create_all.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch


# ── 1. sqlite env skips the check ────────────────────────────────────────────

def test_alembic_check_skips_on_sqlite(tmp_path) -> None:
    """check_alembic_heads is a no-op when DATABASE_URL contains 'sqlite'."""
    from app.startup_checks import check_alembic_heads

    # Should complete without any alembic / DB I/O
    check_alembic_heads(database_url="sqlite:///test.db")  # no raise


# ── 2. non-sqlite + head mismatch raises ─────────────────────────────────────

def test_alembic_check_raises_on_head_mismatch() -> None:
    """Simulated pg URL with stale heads raises RuntimeError."""
    from app.startup_checks import check_alembic_heads

    fake_script = MagicMock()
    fake_revision = MagicMock()
    fake_revision.revision = "aabbccdd1234"
    fake_script.get_revisions.return_value = [fake_revision]

    fake_ctx = MagicMock()
    fake_ctx.get_current_heads.return_value = ("00000000dead",)  # stale

    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_engine = MagicMock()
    fake_engine.connect.return_value = fake_conn

    # The imports inside check_alembic_heads are local — patch at module level
    with (
        patch("alembic.config.Config"),
        patch("alembic.script.ScriptDirectory") as mock_sd,
        patch("alembic.runtime.migration.MigrationContext") as mock_mc,
        patch("sqlalchemy.create_engine", return_value=fake_engine),
    ):
        mock_sd.from_config.return_value = fake_script
        mock_mc.configure.return_value = fake_ctx

        import pytest
        with pytest.raises(RuntimeError, match="NOT at alembic head"):
            check_alembic_heads(database_url="postgresql://user:pass@host/db")


# ── 3. grep: no create_all in main.py ────────────────────────────────────────

def test_no_create_all_in_main() -> None:
    """app/main.py must not contain Base.metadata.create_all."""
    main_py = Path(__file__).resolve().parents[1] / "app" / "main.py"
    content = main_py.read_text()
    assert "Base.metadata.create_all" not in content, (
        "app/main.py still contains Base.metadata.create_all — issue #21 not fully fixed"
    )


# ── 4. create_app calls check_alembic_heads (not create_all) ─────────────────

def test_create_app_calls_check_alembic_heads(monkeypatch) -> None:
    """create_app() must invoke check_alembic_heads() (no-op in sqlite env)."""
    called = []

    import app.startup_checks as sc
    monkeypatch.setattr(sc, "check_alembic_heads", lambda: called.append("called"))

    import app.main as main_mod
    importlib.reload(main_mod)
    app_obj = main_mod.create_app()

    assert "called" in called, (
        "create_app() did not call check_alembic_heads() — issue #21 regression"
    )
