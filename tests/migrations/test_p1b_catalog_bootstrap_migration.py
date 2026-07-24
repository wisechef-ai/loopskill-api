"""tests/migrations/test_p1b_catalog_bootstrap_migration.py

Phase 1b — alembic chain fix: new bootstrap catalog migration (e0f1a2b3c4d5).

Why this test exists
--------------------
Before this fix, ``alembic upgrade head`` on a fresh SQLite DB crashed at
revision ``a7f7db696591`` (typed_telemetry_and_carousel) with:

    OperationalError: no such table: telemetry_events

The root cause: ``telemetry_events``, ``carousel_entries``, ``skills``, and
``install_events`` were created out-of-band by ``Base.metadata.create_all()``
in production history.  No migration ever bootstrapped them, so the ALTER
TABLE calls in ``a7f7db696591`` exploded on a fresh DB.

The fix inserts migration ``e0f1a2b3c4d5`` between the baseline (``4ba0bf05cd47``)
and ``a7f7db696591``, creating those 4 tables with IF-NOT-EXISTS guards before
any migration tries to ALTER them.

DoD checklist verified here
---------------------------
#1  alembic upgrade head on fresh SQLite → exit 0
#2  alembic downgrade base on fully-upgraded SQLite → exit 0
#3  alembic heads → exactly 1 head
#6  new migration verified in isolation (upgrade → downgrade, tables appear / disappear)
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).parent.parent.parent

NEW_REVISION = "e0f1a2b3c4d5"
BASELINE_REVISION = "4ba0bf05cd47"

# The 4 tables that the new migration must create.
BOOTSTRAP_TABLES: frozenset[str] = frozenset(
    {"skills", "telemetry_events", "carousel_entries", "install_events"}
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _alembic_cfg(db_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _table_names(engine: sa.Engine) -> set[str]:
    with engine.connect() as conn:
        inspector = sa.inspect(conn)
        return set(inspector.get_table_names())


def _run_alembic(args: list[str], db_url: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "WR_DATABASE_URL": db_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


# ── DoD #6 — isolation proof ──────────────────────────────────────────────────


class TestBootstrapMigrationIsolation:
    """DoD #6: new migration's DDL verified in isolation (in-process, no subprocess)."""

    def test_upgrade_to_new_revision_creates_tables(self, tmp_path: Path, monkeypatch) -> None:
        """upgrade(e0f1a2b3c4d5) must create all 4 baseline catalog tables."""
        db_url = f"sqlite:///{tmp_path / 'iso.db'}"
        engine = sa.create_engine(db_url)
        cfg = _alembic_cfg(db_url)

        # alembic/env.py resolves the URL from WR_DATABASE_URL when set,
        # which overrides cfg's sqlalchemy.url. conftest sets WR_DATABASE_URL
        # to the shared test DB, so without this the in-process command.upgrade
        # would write to that DB instead of our isolated tmp db. Pin the env to
        # the same URL the test's engine inspects.
        monkeypatch.setenv("WR_DATABASE_URL", db_url)
        command.upgrade(cfg, NEW_REVISION)

        tables = _table_names(engine)
        missing = BOOTSTRAP_TABLES - tables
        assert not missing, (
            f"Tables missing after upgrade to {NEW_REVISION}: {sorted(missing)}. "
            f"All tables present: {sorted(tables)}"
        )
        engine.dispose()

    def test_downgrade_from_new_revision_drops_tables(self, tmp_path: Path, monkeypatch) -> None:
        """downgrade(4ba0bf05cd47) after upgrade(e0f1a2b3c4d5) must remove all 4 tables."""
        db_url = f"sqlite:///{tmp_path / 'iso.db'}"
        engine = sa.create_engine(db_url)
        cfg = _alembic_cfg(db_url)

        monkeypatch.setenv("WR_DATABASE_URL", db_url)
        command.upgrade(cfg, NEW_REVISION)
        command.downgrade(cfg, BASELINE_REVISION)

        tables = _table_names(engine)
        still_present = BOOTSTRAP_TABLES & tables
        assert not still_present, f"Tables still present after downgrade to baseline: {sorted(still_present)}"
        engine.dispose()


# ── DoD #1 and #2 — full chain on fresh SQLite ────────────────────────────────


class TestFullChainSQLite:
    """DoD #1/#2: full alembic chain (upgrade head + downgrade base) on a fresh SQLite DB.

    Uses subprocess so alembic reads alembic.ini from REPO_ROOT and picks up
    WR_DATABASE_URL from the environment, matching exactly the bootstrap.py flow.
    """

    def test_upgrade_head_fresh_sqlite_exits_zero(self, tmp_path: Path) -> None:
        """DoD #1: alembic upgrade head on a brand-new SQLite DB must exit 0."""
        db_url = f"sqlite:///{tmp_path / 'fresh.db'}"
        r = _run_alembic(["upgrade", "head"], db_url)
        assert r.returncode == 0, (
            f"alembic upgrade head failed on fresh SQLite:\n"
            f"STDOUT:\n{r.stdout[-3000:]}\n"
            f"STDERR:\n{r.stderr[-3000:]}"
        )

    def test_downgrade_base_fully_upgraded_sqlite_exits_zero(self, tmp_path: Path) -> None:
        """DoD #2: alembic downgrade base on a fully upgraded SQLite DB must exit 0."""
        db_url = f"sqlite:///{tmp_path / 'fresh.db'}"

        r = _run_alembic(["upgrade", "head"], db_url)
        assert (
            r.returncode == 0
        ), f"Prerequisite upgrade head failed — can't test downgrade:\n{r.stderr[-2000:]}"

        r = _run_alembic(["downgrade", "base"], db_url)
        assert r.returncode == 0, (
            f"alembic downgrade base failed:\n"
            f"STDOUT:\n{r.stdout[-3000:]}\n"
            f"STDERR:\n{r.stderr[-3000:]}"
        )


# ── DoD #3 — single head ──────────────────────────────────────────────────────


class TestSingleHead:
    """DoD #3: the migration DAG must have exactly one head after the repoint."""

    def test_exactly_one_alembic_head(self) -> None:
        """Repointing a7f7db696591 must not introduce a branch — still exactly 1 head."""
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        sd = ScriptDirectory.from_config(cfg)
        heads = sd.get_heads()
        assert len(heads) == 1, (
            f"Expected exactly 1 alembic head but got {len(heads)}: {heads}. "
            "Likely the new migration's down_revision is wrong or a7f7db696591 "
            "was not repointed to the new revision."
        )
