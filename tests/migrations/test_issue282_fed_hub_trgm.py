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


# Private, session-scoped table the RED-proof seeds instead of the shared
# ``federation_hub_skills``. See _build_private_legacy_table for why.
_PRIVATE_REDPROOF_TABLE = "issue282_redproof_skills"

# The proof's prod-scale row count (issue #282 measured the bug at ~90k).
_REDPROOF_ROWS = 90_000


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


def _seed_prod_scale(conn: sa.Connection, n: int = 90_000, table: str = "federation_hub_skills") -> None:
    conn.execute(sa.text(f"DELETE FROM {table} WHERE slug LIKE 'issue282-seed-%'"))
    conn.execute(
        sa.text(
            f"""
            INSERT INTO {table} (slug, title, description, source, install_path)
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
    conn.execute(sa.text(f"ANALYZE {table}"))


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

    SELF-CONTAINED — issues #454 / #426 (card t_0fa474c0)
    -----------------------------------------------------
    This asserts a *planner decision*, and the planner prices a seq scan off
    ``pg_class.reltuples`` / ``relpages``. ``federation_hub_skills`` is shared
    by the whole suite: five other test files write it, and this file's own
    ``TestPostgresIndexScan`` seeds 90k rows then plain-``DELETE``s them, so
    dead tuples stay and ``relpages`` roughly triples. Under pytest-randomly
    those two numbers became "whichever tests ran first", so the plan — and
    this assertion — flipped at random (postgres legs: seeds 1171916253,
    2022454211, 3107078303, 3342256982, 568494569) while passing in a fresh
    database. Reproduce the old flake deterministically in serial order:
    ``TestPostgresIndexScan::test_explain_shows_index_scan_not_seq_scan``
    followed by this test (per #426).

    The fix is isolation, not a nudge. ``SET enable_bitmapscan=off`` would
    *force* the very plan the proof exists to discover, and asserting on
    pinned ``pg_stats`` still trusts a table this test does not own. So the
    proof builds the retracted design on a private, session-scoped TEMP
    table: same column types (copied from the real table, so the shape can
    never drift from what ships), no history, never autovacuumed, and dropped
    with the session even if the test crashes. Same shape + same 90k rows =>
    the same plan under every shuffle.

    Everything (build, seed, ANALYZE, EXPLAIN) runs on ONE connection — TEMP
    relations are session-scoped, so a second pooled connection would not see
    the table at all.
    """

    # A prod-scale heap in this row shape is ~2.5k pages. A relation that was
    # never ANALYZEd reports reltuples=0/relpages=0, and the planner seq-scans
    # an "empty" table for free — the proof would then "pass" without proving
    # anything. These floors are the anti-false-positive gate, not a tuning
    # knob: they fail loudly if seeding or stats never landed.
    _MIN_PAGES = 500
    _MIN_ROWS = int(_REDPROOF_ROWS * 0.9)

    _OR_QUERY = (
        "EXPLAIN (FORMAT JSON) SELECT slug, title FROM "
        + _PRIVATE_REDPROOF_TABLE
        + " WHERE title ILIKE :like OR slug ILIKE :like OR description ILIKE :like "
        "ORDER BY title ASC LIMIT 20"
    )

    @classmethod
    def _build_legacy_design(cls, conn: sa.Connection) -> None:
        """Create the retracted 3-index design + its prod-scale data.

        Runs inside the caller's transaction so a crash leaves nothing behind.
        """
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(sa.text(f"DROP TABLE IF EXISTS pg_temp.{_PRIVATE_REDPROOF_TABLE}"))
        # Types come from the real table (WHERE false is a one-time filter —
        # no heap scan), so the proof cannot silently drift from prod shape.
        conn.execute(
            sa.text(
                f"CREATE TEMP TABLE {_PRIVATE_REDPROOF_TABLE} AS "
                "SELECT slug, title, description, source, install_path "
                "FROM federation_hub_skills WHERE false"
            )
        )
        for name, col in _LEGACY_INDEXES.items():
            conn.execute(
                sa.text(f"CREATE INDEX {name} ON {_PRIVATE_REDPROOF_TABLE} USING gin ({col} gin_trgm_ops)")
            )
        _seed_prod_scale(conn, n=_REDPROOF_ROWS, table=_PRIVATE_REDPROOF_TABLE)

    @staticmethod
    def _plan_to_lines(node: dict) -> list[str]:
        """Flatten an EXPLAIN (FORMAT JSON) plan tree into readable lines."""
        lines: list[str] = []

        def walk(n: dict) -> None:
            rel = n.get("Relation Name")
            label = f"{n['Node Type']} on {rel}" if rel else n["Node Type"]
            lines.append(
                f"{label} (cost={n.get('Startup Cost', 0):.2f}..{n.get('Total Cost', 0):.2f}"
                f" rows={n.get('Plan Rows', 0)})"
            )
            for child in n.get("Plans") or []:
                walk(child)

        walk(node)
        return lines

    @classmethod
    def _capture_proof(cls, conn: sa.Connection) -> dict:
        """EXPLAIN the legacy OR query on the private table, with diagnostics.

        Returns the row/stats/plan/cost tuple every assertion in this class
        reads, so a failure always names the state it saw instead of a bare
        plan diff (the #426/#454 triage tax was exactly this).
        """
        # Second ANALYZE, deliberately AFTER the build transaction committed:
        # the one inside _seed_prod_scale runs over rows still invisible to
        # other snapshots, so the authoritative reltuples/relpages are these.
        conn.execute(sa.text(f"ANALYZE {_PRIVATE_REDPROOF_TABLE}"))

        stats = (
            conn.execute(
                sa.text(
                    "SELECT c.reltuples::bigint AS reltuples, c.relpages AS relpages, "
                    f"       (SELECT count(*) FROM pg_temp.{_PRIVATE_REDPROOF_TABLE}) AS n_rows, "
                    "       (SELECT count(*) FROM pg_class i "
                    "         WHERE i.relname = ANY(:idx_names) AND i.relkind = 'i') AS n_indexes "
                    "FROM pg_class c WHERE c.relname = :tbl"
                ),
                {"idx_names": list(_LEGACY_INDEXES), "tbl": _PRIVATE_REDPROOF_TABLE},
            )
            .mappings()
            .one()
        )

        def explain(seqscan_off: bool = False) -> dict:
            if seqscan_off:
                conn.execute(sa.text("SET enable_seqscan = off"))
            try:
                js = conn.execute(sa.text(cls._OR_QUERY), {"like": "%security%"}).scalar()
            finally:
                if seqscan_off:
                    conn.execute(sa.text("RESET enable_seqscan"))
            return js[0]["Plan"] if isinstance(js, list) else js["Plan"]

        plan = explain()
        # What the planner would have paid for the best index-only path. Kept
        # for the failure message: it is the number that says whether the 3
        # legacy indexes were genuinely unattractive, or merely unlucky.
        alt_plan = explain(seqscan_off=True)
        return {
            "n_rows": stats["n_rows"],
            "reltuples": stats["reltuples"],
            "relpages": stats["relpages"],
            "n_indexes": stats["n_indexes"],
            "plan": plan,
            "plan_text": "\n".join(cls._plan_to_lines(plan)),
            "alt_plan_text": "\n".join(cls._plan_to_lines(alt_plan)),
            "seq_cost": plan["Total Cost"],
            "alt_cost": alt_plan["Total Cost"],
        }

    @classmethod
    def _assert_red_proof(cls, proof: dict) -> None:
        # Preconditions FIRST — a seed/stats problem must name itself rather
        # than arrive disguised as a planner surprise (the #426 failure mode).
        assert proof["n_rows"] == _REDPROOF_ROWS, (
            f"private RED-proof table must hold exactly {_REDPROOF_ROWS} seed rows, got {proof['n_rows']}"
        )
        assert proof["n_indexes"] == len(_LEGACY_INDEXES), (
            f"RED-proof must be pricing against all {len(_LEGACY_INDEXES)} legacy "
            f"per-column trigram indexes, found {proof['n_indexes']}"
        )
        assert proof["reltuples"] >= cls._MIN_ROWS, (
            f"ANALYZE never landed on the private table (reltuples={proof['reltuples']} "
            f"for {proof['n_rows']} rows) — a table the planner believes is empty "
            "seq-scans for free and this proof would prove nothing"
        )
        assert proof["relpages"] >= cls._MIN_PAGES, (
            f"private table is not prod-scale (relpages={proof['relpages']}, "
            f"floor {cls._MIN_PAGES}) — the bug only reproduces at prod scale"
        )
        assert f"Seq Scan on {_PRIVATE_REDPROOF_TABLE}" in proof["plan_text"], (
            "Expected the RED-proof to reproduce the bug (seq scan despite "
            f"3 legacy indexes present) but got:\n{proof['plan_text']}\n"
            f"— private table: rows={proof['n_rows']} reltuples={proof['reltuples']} "
            f"relpages={proof['relpages']}\n"
            f"— chosen seq-scan cost={proof['seq_cost']}, "
            f"best index path (enable_seqscan=off) cost={proof['alt_cost']}:\n"
            f"{proof['alt_plan_text']}"
        )

    @classmethod
    def _run_proof(cls, url: str) -> None:
        """Build the private design and assert the planner still seq-scans."""
        # ONE connection for the whole proof: TEMP objects are session-scoped.
        engine = sa.create_engine(url)
        try:
            conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            try:
                with conn.begin():
                    cls._build_legacy_design(conn)
                cls._assert_red_proof(cls._capture_proof(conn))
            finally:
                conn.close()
        finally:
            engine.dispose()  # closes the session, which drops the TEMP table

    def test_three_column_indexes_with_or_still_seq_scans(self, tmp_path, monkeypatch) -> None:
        url = _postgres_test_url()
        if url is None:
            pytest.skip("no reachable Postgres — set TEST_DATABASE_URL to a postgres:// DSN to run this test")
        cfg = _alembic_cfg(url)
        monkeypatch.setenv("WR_DATABASE_URL", url)  # scoped: a raw write leaked into later tests
        # Ensures the shared table exists (fresh CI DB) so the CTAS below can
        # copy its column types, and pins the pre-fix state of the chain.
        command.upgrade(cfg, PRIOR_REVISION)
        self._run_proof(url)

    def test_red_proof_is_immune_to_shared_table_state(self, tmp_path, monkeypatch) -> None:
        """Regression guard for #454 / #426.

        Replays the exact pollution that used to flip the proof — a prior test
        seeding then plain-DELETEing 90k rows (dead tuples, inflated
        ``relpages``) plus rows left behind that actually match the search
        term (what the trigram indexes would chase) — and requires the proof to
        be unmoved. On the pre-fix code this poisons the shared table's cost
        model and the assertion fails; on the isolated proof it cannot.
        """
        url = _postgres_test_url()
        if url is None:
            pytest.skip("no reachable Postgres — set TEST_DATABASE_URL to a postgres:// DSN to run this test")
        cfg = _alembic_cfg(url)
        monkeypatch.setenv("WR_DATABASE_URL", url)
        command.upgrade(cfg, PRIOR_REVISION)

        engine = sa.create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
                _seed_prod_scale(conn, n=_REDPROOF_ROWS)  # 90k live rows in the SHARED table
                conn.execute(sa.text("DELETE FROM federation_hub_skills WHERE slug LIKE 'issue282-seed-%'"))
                conn.execute(
                    sa.text(
                        "INSERT INTO federation_hub_skills "
                        "(slug, title, description, source, install_path) "
                        "SELECT 'issue282-poison-' || g, "
                        "       'Security Skill ' || g, "
                        "       'security hardened deployment playbook ' || md5(random()::text), "
                        "       'hermes-hub', 'fetch_origin' "
                        "FROM generate_series(1, :n) g"
                    ),
                    {"n": 5_000},
                )
                conn.execute(sa.text("ANALYZE federation_hub_skills"))
            try:
                self._run_proof(url)
            finally:
                with engine.begin() as conn:
                    conn.execute(
                        sa.text("DELETE FROM federation_hub_skills WHERE slug LIKE 'issue282-poison-%'")
                    )
                # Hand the next test back the heap it expects. Plain VACUUM,
                # not VACUUM FULL (#427's approach): FULL takes ACCESS
                # EXCLUSIVE and would stall the other xdist workers that share
                # this DB under --dist loadfile. VACUUM truncates the dead
                # tail pages (our seed rows are the last written) and refreshes
                # relpages, which is all the shared table's cost model needs.
                vac = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
                try:
                    vac.execute(sa.text("VACUUM (ANALYZE) federation_hub_skills"))
                finally:
                    vac.close()
        finally:
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
    def pg_session(self, tmp_path, monkeypatch):
        url = _postgres_test_url()
        if url is None:
            pytest.skip("no reachable Postgres — set TEST_DATABASE_URL to a postgres:// DSN to run this test")
        cfg = _alembic_cfg(url)
        monkeypatch.setenv("WR_DATABASE_URL", url)  # scoped: a raw write leaked into later tests
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
    def test_downgrade_drops_index_upgrade_recreates_it(self, tmp_path, monkeypatch) -> None:
        url = _postgres_test_url()
        if url is None:
            pytest.skip("no reachable Postgres — set TEST_DATABASE_URL to a postgres:// DSN to run this test")
        cfg = _alembic_cfg(url)
        monkeypatch.setenv("WR_DATABASE_URL", url)  # scoped: a raw write leaked into later tests
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
    def pg_engine(self, monkeypatch):
        url = _postgres_test_url()
        if url is None:
            pytest.skip("no reachable Postgres — set TEST_DATABASE_URL to a postgres:// DSN to run this test")
        cfg = _alembic_cfg(url)
        monkeypatch.setenv("WR_DATABASE_URL", url)  # scoped: a raw write leaked into later tests
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
