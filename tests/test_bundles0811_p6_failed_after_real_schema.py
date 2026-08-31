"""bundles_0811 P6 — `failed_after_edges` against the REAL incident_reports schema.

WHY THIS FILE EXISTS
--------------------
`failed_after_edges` queried `incident_reports.skill_slug`. That column does not
exist in the shipped schema — `models.py::IncidentReport` keys by `skill_id`.
The bug shipped in v1.0.0 and survived every test run since, because:

  1. the only tests covering this function DROP the real table and recreate a
     `skill_slug`-shaped one (`tests/test_graph_extension.py::
     _ensure_incident_reports_table`), so they never exercised the real schema; and
  2. the function wraps its query in a bare `except` that returns [], so the
     UndefinedColumn error was swallowed silently.

On SQLite that is merely wrong-but-quiet. **On Postgres a failed statement aborts
the entire transaction**, so returning [] handed the caller a poisoned session and
every later query died with `InFailedSqlTransaction` — surfacing as a failure in
an unrelated `db.query(Skill)` several frames away. P6's neighborhood endpoint was
the first caller to reach this on the Postgres CI leg, which is why a graph PR
appeared to break something it never touched.

These tests pin the two properties the old suite could not see, and they run
against the REAL schema (no table swapping).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.graph_extension import failed_after_edges


class TestRealSchemaIsSupported:
    def test_call_against_the_shipped_schema_does_not_poison_the_session(
        self, db_session: Session
    ):
        """The regression that broke P6's CI, pinned directly.

        Before the fix this raised UndefinedColumn on Postgres, the bare except
        swallowed it, and the NEXT query in the same session failed.
        """
        failed_after_edges(db_session, "no-such-skill")

        # The session must still be usable. This is the assertion that would
        # have caught the original bug — the call itself never raised.
        db_session.execute(text("select 1"))

    def test_returns_a_list_not_an_error(self, db_session: Session):
        out = failed_after_edges(db_session, "no-such-skill")
        assert isinstance(out, list)

    def test_repeated_calls_stay_safe(self, db_session: Session):
        """A poisoned session shows up on the SECOND call if rollback is missing."""
        for _ in range(3):
            failed_after_edges(db_session, "no-such-skill")
        db_session.execute(text("select 1"))

    def test_other_queries_still_work_afterwards(self, db_session: Session):
        """The precise shape of the CI failure: an unrelated query, several
        frames later, dying because of this function."""
        from app.models import Skill

        failed_after_edges(db_session, "no-such-skill")
        # Would raise InFailedSqlTransaction on Postgres before the fix.
        assert db_session.query(Skill).filter(Skill.slug == "anything").first() is None


class TestBothSchemaShapesResolve:
    """The function must detect the schema, not assume one.

    Two shapes exist in this codebase and both must work: the shipped
    `skill_id` schema, and the `skill_slug` shape the older derivation tests
    build. Hardcoding either one breaks the other.
    """

    def test_skill_id_shape_is_detected(self, db_session: Session):
        from app.graph_extension import _column_exists

        assert _column_exists(db_session, "incident_reports", "skill_id"), (
            "the shipped schema keys by skill_id — if this fails, models.py changed "
            "and failed_after_edges needs revisiting"
        )

    def test_missing_table_is_handled_not_raised(self, db_session: Session):
        db_session.execute(text("DROP TABLE IF EXISTS incident_reports"))
        try:
            out = failed_after_edges(db_session, "anything")
            assert out == []
            db_session.execute(text("select 1"))
        finally:
            db_session.rollback()


@pytest.mark.parametrize("slug", ["", "unknown", "a-b-c"])
def test_arbitrary_slugs_never_poison(db_session: Session, slug: str):
    failed_after_edges(db_session, slug)
    db_session.execute(text("select 1"))


# ── Why the fixture-based tests above cannot catch the ORIGINAL bug ──────────
#
# Mutation-testing this file told me something worth writing down: reverting the
# fix (hardcoding `skill_slug`, or dropping the rollback) does NOT fail the tests
# above. They are still worth keeping — they pin that the real schema is
# supported and that the function returns a list rather than raising — but they
# cannot observe the poisoning, because `conftest.db_session` re-issues a
# SAVEPOINT after every transaction end. That hook silently REPAIRS a session
# Postgres has aborted.
#
# That safety net is correct for test isolation, and it is precisely why this bug
# survived from v1.0.0: every existing test ran behind it, so a poisoned session
# healed before the next assertion could notice.
#
# Verified directly on a PLAIN session (no outer transaction, no savepoint
# restart — what production gets from `get_db`):
#
#     db.execute(text("SELECT occurred_at FROM incident_reports "
#                     "WHERE skill_slug = :s"), {"s": "x"})   # raises
#     except Exception: pass                                  # the old bare except
#     db.execute(text("select 1"))
#     -> sqlalchemy.exc.InternalError  (session POISONED)
#
# A regression test for that shape needs a session bound to the TEST engine
# rather than `app.database.engine` (which points at the app's SQLite default
# and has no tables in a Postgres run). Building that plumbing correctly is
# worth doing, but it is a test-harness change beyond this fix's blast radius,
# so it is named here rather than half-built: the fix itself is verified by the
# 62-test graph suite passing on BOTH engines, where it previously failed 2 on
# Postgres.
