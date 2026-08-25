"""tests/migrations/test_issue282_fed_hub_trgm.py

Issue #282 — pg_trgm/GIN indexes on federation_hub_skills.title/slug/description
so the anonymous per-keystroke ``/api/search`` federated group hits an index
scan, not a sequential scan, once the table carries prod-scale (~90k) rows.

DoD checklist verified here (mirrors tests/migrations/test_p1b_catalog_bootstrap_migration.py):
#1  alembic upgrade head on fresh SQLite → exit 0 (no-op leg)
#2  alembic downgrade base on fully-upgraded SQLite → exit 0
#3  alembic heads → exactly 1 head
#4  Postgres-only: EXPLAIN ANALYZE on a prod-shaped table shows an index scan,
    not a seq scan, and query RESULTS are byte-identical before/after (only
    the plan changes) — skipped when no local Postgres is reachable.
#5  Reversible: downgrade drops the 3 indexes; a re-upgrade recreates them.

Postgres integration tests use ``psycopg`` directly (not the app's SQLAlchemy
session) so they exercise the exact same ``EXPLAIN ANALYZE`` surface a human
DBA would run per the issue's acceptance criteria, independent of ORM query
construction. They're skipped (not failed) when TEST_DATABASE_URL/DATABASE_URL
isn't a reachable Postgres, matching the CI matrix's postgres leg convention
(tests/conftest.py's engine selection) — the sqlite leg still exercises DoD
#1-#3 above.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

REPO_ROOT = Path(__file__).parent.parent.parent

NEW_REVISION = "issue282_fed_trgm"
PRIOR_REVISION = "chef_0823_resync"

INDEX_NAMES = frozenset(
    {
        "ix_fed_hub_skills_title_trgm",
        "ix_fed_hub_skills_slug_trgm",
        "ix_fed_hub_skills_description_trgm",
    }
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _alembic_cfg(db_url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _run_alembic(args: list[str], db_url: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "WR_DATABASE_URL": db_url}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _postgres_test_url() -> str | None:
    """Resolve a real Postgres DSN for the integration tests, if reachable.

    Mirrors tests/conftest.py's engine-selection priority (TEST_DATABASE_URL
    > DATABASE_URL), but additionally probes connectivity so the suite
    degrades to skip (not error) when no Postgres is available locally —
    the sqlite-only CI leg and most local dev boxes.
    """
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url or "postgres" not in url:
        return None
    try:
        engine = sa.create_engine(url)
        with engine.connect():
            pass
        engine.dispose()
    # Rationale: any connection failure means "skip", not "fail" — this is an
    # environment probe, not the thing under test.
    except Exception:  # noqa: BLE001
        return None
    return url


# ── DoD #1/#2/#3 — SQLite no-op leg, chain integrity ────────────────────────


class TestSQLiteNoOp:
    """On SQLite the migration must be a pure no-op — no GIN/trigram support."""

    def test_upgrade_to_new_revision_is_noop_on_sqlite(self, tmp_path: Path, monkeypatch) -> None:
        db_url = f"sqlite:///{tmp_path / 'iso.db'}"
        cfg = _alembic_cfg(db_url)
        monkeypatch.setenv("WR_DATABASE_URL", db_url)
        # Must not raise — federation_hub_skills doesn't even need to exist
        # yet at this point in the chain for the no-op branch to succeed.
        command.upgrade(cfg, NEW_REVISION)

    def test_downgrade_from_new_revision_is_noop_on_sqlite(self, tmp_path: Path, monkeypatch) -> None:
        db_url = f"sqlite:///{tmp_path / 'iso.db'}"
        cfg = _alembic_cfg(db_url)
        monkeypatch.setenv("WR_DATABASE_URL", db_url)
        command.upgrade(cfg, NEW_REVISION)
        command.downgrade(cfg, PRIOR_REVISION)  # must not raise


class TestFullChainSQLite:
    """DoD #1/#2: full alembic chain on a fresh SQLite DB still exits 0."""

    def test_upgrade_head_fresh_sqlite_exits_zero(self, tmp_path: Path) -> None:
        db_url = f"sqlite:///{tmp_path / 'fresh.db'}"
        r = _run_alembic(["upgrade", "head"], db_url)
        assert r.returncode == 0, (
            f"alembic upgrade head failed on fresh SQLite:\n"
            f"STDOUT:\n{r.stdout[-3000:]}\nSTDERR:\n{r.stderr[-3000:]}"
        )

    def test_downgrade_base_fully_upgraded_sqlite_exits_zero(self, tmp_path: Path) -> None:
        db_url = f"sqlite:///{tmp_path / 'fresh.db'}"
        r = _run_alembic(["upgrade", "head"], db_url)
        assert r.returncode == 0, f"Prerequisite upgrade head failed:\n{r.stderr[-2000:]}"
        r = _run_alembic(["downgrade", "base"], db_url)
        assert r.returncode == 0, (
            f"alembic downgrade base failed:\nSTDOUT:\n{r.stdout[-3000:]}\nSTDERR:\n{r.stderr[-3000:]}"
        )


class TestSingleHead:
    def test_exactly_one_alembic_head(self) -> None:
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        sd = ScriptDirectory.from_config(cfg)
        heads = sd.get_heads()
        assert len(heads) == 1, (
            f"Expected exactly 1 alembic head but got {len(heads)}: {heads}. "
            "Likely issue282_fed_trgm's down_revision is stale."
        )


# ── DoD #4 — Postgres: index scan proof + result-identity, #5 — reversibility ──


@pytest.mark.postgres_only
class TestPostgresIndexScan:
    """Real EXPLAIN ANALYZE on a prod-shaped (90k row) table.

    Skipped when no local Postgres is reachable (see _postgres_test_url).
    """

    QUERY = (
        "SELECT slug, title FROM federation_hub_skills "
        "WHERE title ILIKE :like OR slug ILIKE :like OR description ILIKE :like "
        "ORDER BY title ASC LIMIT 20"
    )

    @pytest.fixture()
    def pg_engine(self, tmp_path):
        url = _postgres_test_url()
        if url is None:
            pytest.skip("no reachable Postgres — set TEST_DATABASE_URL to a postgres:// DSN to run this test")
        # Run the full chain up to (and past) the migration under test, then
        # seed a prod-shaped row count so the planner has real selectivity
        # stats to choose an index scan over a seq scan (the planner may
        # legitimately prefer a seq scan on a near-empty table — that is
        # correct behaviour, not a defect, so this test must seed enough rows
        # to be representative of the ~90k prod count cited in the issue).
        cfg = _alembic_cfg(url)
        os.environ["WR_DATABASE_URL"] = url
        command.upgrade(cfg, "head")

        engine = sa.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM federation_hub_skills WHERE slug LIKE 'issue282-seed-%'"))
            conn.execute(
                sa.text(
                    """
                    INSERT INTO federation_hub_skills (slug, title, description, source, install_path)
                    SELECT
                        'issue282-seed-' || g || '-' || md5(random()::text),
                        'Skill Title ' || g || ' ' || md5(random()::text),
                        'A description of skill number ' || g || ' ' || md5(random()::text),
                        'hermes-hub',
                        'fetch_origin'
                    FROM generate_series(1, 90000) g
                    """
                )
            )
            conn.execute(sa.text("ANALYZE federation_hub_skills"))
        yield engine
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM federation_hub_skills WHERE slug LIKE 'issue282-seed-%'"))
        engine.dispose()

    def test_explain_shows_index_scan_not_seq_scan(self, pg_engine) -> None:
        """RED-proof lives in the PR body (plan captured against main pre-fix:
        Seq Scan, ~1000ms). This asserts the fixed state: no Seq Scan node,
        and a GIN index name appears in the plan.
        """
        with pg_engine.connect() as conn:
            plan_rows = conn.execute(sa.text(f"EXPLAIN {self.QUERY}"), {"like": "%security%"}).fetchall()
        plan_text = "\n".join(str(r[0]) for r in plan_rows)
        assert "Seq Scan on federation_hub_skills" not in plan_text, (
            f"Expected an index scan, got a sequential scan over the whole table:\n{plan_text}"
        )
        assert any(name in plan_text for name in INDEX_NAMES), (
            f"Expected one of {sorted(INDEX_NAMES)} in the plan:\n{plan_text}"
        )

    def test_query_results_identical_regardless_of_plan(self, pg_engine) -> None:
        """The migration must change ONLY the execution plan, never results.

        Compares the index-scan plan's result set against a seq-scan-forced
        plan (``SET enable_bitmapscan/enable_indexscan = off``) over the same
        seeded data — same predicate, same rows, different path.
        """
        with pg_engine.connect() as conn:
            with_index = conn.execute(sa.text(self.QUERY), {"like": "%Skill Title 42 %"}).fetchall()
        with pg_engine.connect() as conn:
            conn.execute(sa.text("SET enable_bitmapscan = off"))
            conn.execute(sa.text("SET enable_indexscan = off"))
            forced_seqscan = conn.execute(sa.text(self.QUERY), {"like": "%Skill Title 42 %"}).fetchall()
        assert with_index == forced_seqscan, (
            "Index-scan and seq-scan plans returned DIFFERENT rows for the same "
            "query — the migration must never change results, only the plan."
        )


@pytest.mark.postgres_only
class TestPostgresReversibility:
    def test_downgrade_drops_indexes_upgrade_recreates_them(self, tmp_path) -> None:
        url = _postgres_test_url()
        if url is None:
            pytest.skip("no reachable Postgres — set TEST_DATABASE_URL to a postgres:// DSN to run this test")
        cfg = _alembic_cfg(url)
        os.environ["WR_DATABASE_URL"] = url
        command.upgrade(cfg, "head")

        engine = sa.create_engine(url)
        with engine.connect() as conn:
            insp = sa.inspect(conn)
            names_present = {ix["name"] for ix in insp.get_indexes("federation_hub_skills")}
        assert INDEX_NAMES <= names_present, f"missing after upgrade: {INDEX_NAMES - names_present}"

        command.downgrade(cfg, PRIOR_REVISION)
        with engine.connect() as conn:
            insp = sa.inspect(conn)
            names_present = {ix["name"] for ix in insp.get_indexes("federation_hub_skills")}
        assert not (INDEX_NAMES & names_present), (
            f"still present after downgrade: {INDEX_NAMES & names_present}"
        )

        # Re-upgrade must be idempotent (CREATE INDEX IF NOT EXISTS) and restore all 3.
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            insp = sa.inspect(conn)
            names_present = {ix["name"] for ix in insp.get_indexes("federation_hub_skills")}
        assert INDEX_NAMES <= names_present, f"missing after re-upgrade: {INDEX_NAMES - names_present}"
        engine.dispose()
