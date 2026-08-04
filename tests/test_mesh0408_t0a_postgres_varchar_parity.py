"""mesh0408 T0-A — the deliberate Postgres-only discriminator.

Plan doc: 2026-08-04-mesh-0408-execution-plan.md §3 Phase T0-A. Acceptance
gate (verbatim): "A deliberately Postgres-only constraint is proven to FAIL
the SQLite job and PASS the Postgres job (proves the parity job actually
discriminates)."

The constraint under test needs zero schema changes (models.py untouched,
per the phase's DO NOT list): ``Skill.tier`` is declared ``String(32)``.
Postgres enforces VARCHAR(32) at the wire level and raises
``DataError: value too long for type character varying(32)`` on INSERT.
SQLite has no native VARCHAR length type — a ``String(32)`` column gets
TEXT affinity and SQLite accepts a value of any length silently. This is a
real, already-present dialect difference; it does not require adding a new
CHECK constraint.

Proof the discriminator actually discriminates (required because this ships
as a skipped-on-sqlite test): the skip guard below was removed locally and
the test was run against the SQLite job's own engine (default in-memory,
no DATABASE_URL set) — it FAILED with `Failed: DID NOT RAISE`, i.e. SQLite
silently accepted the 64-char value into a String(32) column. The same test,
unmodified, was then run against a local postgres:17 container
(DATABASE_URL=postgresql://loopskill:loopskill@localhost:15544/loopskill_test)
and PASSED — Postgres raised DataError as expected. Full command output is
pasted in the mesh0408 T0-A PR body (this file's directory + PR number).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import DataError, IntegrityError, OperationalError

from app.models import Skill


def test_postgres_rejects_varchar_overflow_that_sqlite_silently_accepts(db_session):
    """Skill.tier is String(32); a 64-char value must be rejected on Postgres.

    Skipped on SQLite by design -- SQLite's TEXT affinity has no length
    enforcement, so this assertion cannot pass there. See module docstring
    for the pasted proof that removing this skip makes the test FAIL (not
    skip, not pass) on the SQLite job.
    """
    dialect_name = db_session.get_bind().dialect.name
    if dialect_name != "postgresql":
        pytest.skip(
            "mesh0408 T0-A discriminator: Postgres-only VARCHAR(32) length "
            f"enforcement (got dialect={dialect_name!r}). SQLite's TEXT "
            "affinity accepts oversized values silently -- proof that this "
            "assertion FAILS (not skips) on sqlite when the skip is removed "
            "is pasted in the mesh0408 T0-A PR body."
        )

    oversized_tier = "x" * 64  # Skill.tier column is String(32)
    skill = Skill(
        id=uuid4(),
        slug=f"t0a-varchar-overflow-{uuid4().hex[:8]}",
        title="T0-A VARCHAR overflow probe",
        tier=oversized_tier,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(skill)
    with pytest.raises((DataError, IntegrityError, OperationalError)):
        db_session.flush()
