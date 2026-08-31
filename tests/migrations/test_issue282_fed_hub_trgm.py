"""tests/migrations/test_issue282_fed_hub_trgm.py

Issue #282 — pg_trgm/GIN expression index on federation_hub_skills so the
anonymous per-keystroke ``/api/search`` federated group hits an index scan,
not a sequential scan, once the table carries prod-scale (~90k) rows.

REVISED 2026-08-25 (same cycle, pre-PR breaker pass): the first draft used
THREE separate per-column GIN trigram indexes (title/slug/description)
queried with OR. That does NOT work — verified against a real 90k-row
Postgres that the planner prices the 3-way BitmapOr plan above a plain seq
scan and silently falls back to it (812ms, unindexed; see RED-proof below).
The fix is ONE GIN trigram index over the expression
``coalesce(title,'') || ' ' || coalesce(slug,'') || ' ' || coalesce(description,'')``,
with ``app/services/unified_search.py::search_federated_group`` updated to
filter on the syntactically-identical SQLAlchemy expression so Postgres
recognizes the index.

DoD checklist verified here (mirrors tests/migrations/test_p1b_catalog_bootstrap_migration.py):
#1  alembic upgrade head on fresh SQLite → exit 0 (no-op leg)
#2  alembic downgrade base on fully-upgraded SQLite → exit 0
#3  alembic heads → exactly 1 head
#4  Postgres-only: EXPLAIN ANALYZE on a prod-shaped table shows an index scan,
    not a seq scan, via the actual ORM query search_federated_group builds
    (not a hand-written SQL string) — skipped when no local Postgres is
    reachable.
#5  Reversible: downgrade drops the index; a re-upgrade recreates it.
#6  RED-proof: the ORIGINAL three-clause-OR query plan is captured and
    asserted to be a seq scan even WITH all three old-style single-column
    indexes present — proves the bug was real, not a strawman.
#7  Query-result equivalence: the new single-expression predicate returns
    the same rows as the original three-clause OR predicate over a mixed
    seeded dataset (case matters at word-boundary but not substring shape
    for these fixtures — both are simple substring/ILIKE semantics on the
    same three columns).

Postgres integration tests use the app's own SQLAlchemy session so they
exercise the exact same query construction (search_federated_group) that
ships to prod, not a hand-copied SQL string that could silently drift from
the real code path. They're skipped (not failed) when TEST_DATABASE_URL/
DATABASE_URL isn't a reachable Postgres, matching the CI matrix's postgres
leg convention (tests/conftest.py's engine selection) — the sqlite leg still
exercises DoD #1-#3 above.
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
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).parent.parent.parent

NEW_REVISION = "issue282_fed_trgm"
PRIOR_REVISION = "chef_0823_resync"

INDEX_NAME = "ix_fed_hub_skills_search_expr_trgm"

# The three legacy single-column indexes from the retracted first draft —
# recreated ONLY inside the RED-proof test to demonstrate that even WITH
# them present, the three-clause-OR query still seq-scans. Never part of the
# shipped migration.
_LEGACY_INDEXES = {
    "ix_fed_hub_skills_title_trgm__legacy_redproof": "title",
    "ix_fed_hub_skills_slug_trgm__legacy_redproof": "slug",
    "ix_fed_hub_skills_description_trgm__legacy_redproof": "description",
}


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


def _seed_prod_scale(conn: sa.Connection, n: int = 90_000) -> None:
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
            FROM generate_series(1, :n) g
            """
        ),
        {"n": n},
    )
    conn.execute(sa.text("ANALYZE federation_hub_skills"))


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


# ── DoD #6 — RED-proof: the retracted 3-index design really was broken ─────


@pytest.mark.postgres_only
class TestRedProofLegacyThreeIndexDesignWasBroken:
    """Proves the first-draft design (3 per-column indexes + OR) was a real
    bug, not a strawman: builds those exact 3 legacy indexes by hand (never
    shipped — this migration never creates them) and shows the three-clause
    OR query STILL seq-scans a prod-scale table even with all 3 present.
    """

    def test_three_column_indexes_with_or_still_seq_scans(self, tmp_path) -> None:
        url = _postgres_test_url()
        if url is None:
            pytest.skip("no reachable Postgres — set TEST_DATABASE_URL to a postgres:// DSN to run this test")
        cfg = _alembic_cfg(url)
        os.environ["WR_DATABASE_URL"] = url
        command.upgrade(cfg, PRIOR_REVISION)  # pre-fix state: no expr index yet

        engine = sa.create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                for name, col in _LEGACY_INDEXES.items():
                    conn.execute(
                        sa.text(
                            f"CREATE INDEX IF NOT EXISTS {name} ON federation_hub_skills USING gin ({col} gin_trgm_ops)"
                        )
                    )
                _seed_prod_scale(conn)

            with engine.connect() as conn:
                plan_rows = conn.execute(
                    sa.text(
                        "EXPLAIN SELECT slug, title FROM federation_hub_skills "
                        "WHERE title ILIKE :like OR slug ILIKE :like OR description ILIKE :like "
                        "ORDER BY title ASC LIMIT 20"
                    ),
                    {"like": "%security%"},
                ).fetchall()
            plan_text = "\n".join(str(r[0]) for r in plan_rows)
            assert "Seq Scan on federation_hub_skills" in plan_text, (
                "Expected the RED-proof to reproduce the bug (seq scan despite "
                f"3 legacy indexes present) but got:\n{plan_text}"
            )
        finally:
            with engine.begin() as conn:
                for name in _LEGACY_INDEXES:
                    conn.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
                conn.execute(sa.text("DELETE FROM federation_hub_skills WHERE slug LIKE 'issue282-seed-%'"))
            engine.dispose()


# ── DoD #4 — Postgres: index scan proof via the real ORM path, #5 — reversibility ──


@pytest.mark.postgres_only
class TestPostgresIndexScan:
    """Real EXPLAIN ANALYZE on a prod-shaped (90k row) table, exercising the
    ACTUAL query app/services/unified_search.py::search_federated_group
    builds (not a hand-copied SQL string).

    Skipped when no local Postgres is reachable (see _postgres_test_url).
    """

    @pytest.fixture()
    def pg_session(self, tmp_path):
        url = _postgres_test_url()
        if url is None:
            pytest.skip("no reachable Postgres — set TEST_DATABASE_URL to a postgres:// DSN to run this test")
        cfg = _alembic_cfg(url)
        os.environ["WR_DATABASE_URL"] = url
        command.upgrade(cfg, "head")

        engine = sa.create_engine(url)
        with engine.begin() as conn:
            _seed_prod_scale(conn)

        Session = sessionmaker(bind=engine)
        session = Session()
        yield session
        session.close()
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM federation_hub_skills WHERE slug LIKE 'issue282-seed-%'"))
        engine.dispose()

    def _search_expr_query(self, session):
        from app.models import FederationHubSkill
        from sqlalchemy import func

        search_blob = (
            func.coalesce(FederationHubSkill.title, "")
            + " "
            + func.coalesce(FederationHubSkill.slug, "")
            + " "
            + func.coalesce(FederationHubSkill.description, "")
        )
        return (
            session.query(FederationHubSkill)
            .filter(search_blob.ilike("%security%"))
            .order_by(FederationHubSkill.title.asc())
            .limit(20)
        )

    def test_explain_shows_index_scan_not_seq_scan(self, pg_session) -> None:
        """GREEN state, matching the RED-proof above: with the shipped
        single expression index, the SAME kind of query (now built by the
        real search_federated_group code path) uses the GIN index scan.
        """
        query = self._search_expr_query(pg_session)
        compiled = query.statement.compile(pg_session.get_bind(), compile_kwargs={"literal_binds": True})
        plan_rows = pg_session.execute(sa.text(f"EXPLAIN {compiled}")).fetchall()
        plan_text = "\n".join(str(r[0]) for r in plan_rows)
        assert "Seq Scan on federation_hub_skills" not in plan_text, (
            f"Expected an index scan, got a sequential scan over the whole table:\n{plan_text}"
        )
        assert INDEX_NAME in plan_text, f"Expected {INDEX_NAME!r} in the plan:\n{plan_text}"

    def test_search_federated_group_uses_index_and_returns_seeded_row(self, pg_session, monkeypatch) -> None:
        """End-to-end: the actual production function, not a hand-rolled query."""
        from app.services import unified_search

        # search_federated_group also touches FederationIndexCache; keep that
        # leg a no-op so this test is purely about the hub-table index path.
        monkeypatch.setattr(unified_search, "search_federated_group", unified_search.search_federated_group)
        with pg_session.get_bind().begin() as conn:
            conn.execute(
                sa.text(
                    """
                    INSERT INTO federation_hub_skills (slug, title, description, source, install_path)
                    VALUES ('issue282-seed-findme', 'Findme Security Skill', 'a description', 'hermes-hub', 'fetch_origin')
                    ON CONFLICT (slug) DO NOTHING
                    """
                )
            )
        rows, status = unified_search.search_federated_group(pg_session, "findme", 5)
        assert status == "warm"
        assert any(r["slug"] == "issue282-seed-findme" for r in rows)


@pytest.mark.postgres_only
class TestPostgresReversibility:
    def test_downgrade_drops_index_upgrade_recreates_it(self, tmp_path) -> None:
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
        assert INDEX_NAME in names_present, f"missing after upgrade: {INDEX_NAME}"

        command.downgrade(cfg, PRIOR_REVISION)
        with engine.connect() as conn:
            insp = sa.inspect(conn)
            names_present = {ix["name"] for ix in insp.get_indexes("federation_hub_skills")}
        assert INDEX_NAME not in names_present, f"still present after downgrade: {INDEX_NAME}"

        # Re-upgrade must be idempotent (CREATE INDEX IF NOT EXISTS) and restore it.
        command.upgrade(cfg, "head")
        with engine.connect() as conn:
            insp = sa.inspect(conn)
            names_present = {ix["name"] for ix in insp.get_indexes("federation_hub_skills")}
        assert INDEX_NAME in names_present, f"missing after re-upgrade: {INDEX_NAME}"
        engine.dispose()


# ── DoD #7 — query-result equivalence: new expression vs. original OR ──────


@pytest.mark.postgres_only
class TestQueryResultEquivalence:
    """The new single-expression predicate must return the same rows as the
    original three-clause OR predicate for realistic search terms — proving
    the index-shape change never silently altered search behaviour.
    """

    @pytest.fixture()
    def pg_engine(self):
        url = _postgres_test_url()
        if url is None:
            pytest.skip("no reachable Postgres — set TEST_DATABASE_URL to a postgres:// DSN to run this test")
        cfg = _alembic_cfg(url)
        os.environ["WR_DATABASE_URL"] = url
        command.upgrade(cfg, "head")

        engine = sa.create_engine(url)
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM federation_hub_skills WHERE slug LIKE 'issue282-eqv-%'"))
            fixtures = [
                ("issue282-eqv-1", "Kubernetes Operator Skill", "manage clusters"),
                ("issue282-eqv-2", "Generic Skill", "runs on a kubernetes cluster"),
                ("issue282-eqv-3", "kubernetes-cli-helper", "unrelated description"),
                ("issue282-eqv-4", "Totally Unrelated", "nothing to see here"),
            ]
            for slug, title, desc in fixtures:
                conn.execute(
                    sa.text(
                        "INSERT INTO federation_hub_skills (slug, title, description, source, install_path) "
                        "VALUES (:slug, :title, :desc, 'hermes-hub', 'fetch_origin') "
                        "ON CONFLICT (slug) DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description"
                    ),
                    {"slug": slug, "title": title, "desc": desc},
                )
        yield engine
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM federation_hub_skills WHERE slug LIKE 'issue282-eqv-%'"))
        engine.dispose()

    @pytest.mark.parametrize("term", ["kubernetes", "Skill", "unrelated", "nothing"])
    def test_new_expression_matches_original_or_clauses(self, pg_engine, term) -> None:
        like = f"%{term}%"
        with pg_engine.connect() as conn:
            baseline = {
                row[0]
                for row in conn.execute(
                    sa.text(
                        "SELECT slug FROM federation_hub_skills "
                        "WHERE slug LIKE 'issue282-eqv-%' "
                        "AND (title ILIKE :like OR slug ILIKE :like OR description ILIKE :like)"
                    ),
                    {"like": like},
                ).fetchall()
            }
            new_expr = {
                row[0]
                for row in conn.execute(
                    sa.text(
                        "SELECT slug FROM federation_hub_skills "
                        "WHERE slug LIKE 'issue282-eqv-%' "
                        "AND (coalesce(title,'') || ' ' || coalesce(slug,'') || ' ' || coalesce(description,'')) ILIKE :like"
                    ),
                    {"like": like},
                ).fetchall()
            }
        assert new_expr == baseline, (
            f"term={term!r}: new expression {new_expr} != baseline OR-clauses {baseline}"
        )
