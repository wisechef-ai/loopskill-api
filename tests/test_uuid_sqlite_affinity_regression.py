"""Regression: UUID columns must be TEXT-affine on SQLite (t_e7f3b8fa).

THE FLAKE. ``app/models.py`` declared every UUID column as
``sqlalchemy.dialects.postgresql.UUID(as_uuid=True)``. On Postgres that is
correct and native. On SQLite, however, that type renders the *literal* type
name ``UUID`` in the DDL — and SQLite column-affinity rules give a type name
containing no CHAR/TEXT/CLOB/BLOB/REAL/FLOA/DOUB/INT keyword **NUMERIC
affinity**. NUMERIC affinity coerces well-formed numeric text at write time:

* a 32-char hex string made only of decimal digits that fits int64
  (e.g. ``00000000000040008000000000000201``) is stored as INTEGER;
* one that overflows int64 (e.g. ``56333796529745751234567890123456``) is
  stored as REAL — and reads back as a Python ``float``.

``uuid4()`` draws os.urandom entropy, so a given uuid4's 32-char hex is
all-decimal with probability (10/16)**32 ≈ 2.9e-7 per call. The test suite
draws O(10^5) uuid4s per run, so a run hits an all-decimal id with probability
~1.5-3%. Such a row, once written through the ORM and read back, hands the
float to ``uuid.UUID()`` and dies with::

    AttributeError: 'float' object has no attribute 'replace'

observed as the random-order sqlite-leg failure of
``tests/test_unified_search_multiword.py::test_single_term_substring_behaviour_unchanged``
(PR #460, job 131011275917, 2026-11-12: read-back value 5.633379652974575e+31 —
a 32-digit all-decimal string rounded to float64). Seed replay could never
reproduce it because uuid4() does not use the seeded ``random`` module —
this is an entropy flake, not an order-dependence; it merely *surfaced* in the
random-order legs because those runs re-roll the whole suite's entropy.

THE FIX. Every UUID column now uses the dialect-agnostic ``sqlalchemy.Uuid``,
which renders ``CHAR(32)`` on SQLite — TEXT affinity stores every id
byte-for-byte as text, numeric-shaped or not — and native ``UUID`` on Postgres
(byte-identical DDL to before). This also removes the same latent bug from the
self-hosted SQLite path in production.

These tests pin the round-trip for every numeric-shaped id a writer could
produce, plus the DDL itself so the postgresql.UUID import cannot quietly
return.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.models import Skill


_ALL_DECIMAL_BIG = "56333796529745751234567890123456"  # > int64 → would be REAL
_ALL_DECIMAL_SMALL = "00000000000040008000000000000201"  # fits int64 → would be INTEGER


def test_all_decimal_uuid_round_trips_orm_read(db_session):
    """The exact CI crash class: all-decimal hex id survives a read-back."""
    special = UUID(_ALL_DECIMAL_BIG)
    db_session.add(Skill(id=special, slug="all-decimal-big", title="t", is_public=True))
    db_session.flush()
    db_session.expire_all()
    back = db_session.query(Skill).filter(Skill.slug == "all-decimal-big").one()
    assert back.id == special  # pre-fix: AttributeError 'float' has no 'replace'


def test_int64_fitting_decimal_uuid_round_trips(db_session):
    """The INTEGER-storage variant: all-decimal hex that fits in int64."""
    special = UUID(_ALL_DECIMAL_SMALL)
    db_session.add(Skill(id=special, slug="all-decimal-small", title="t", is_public=True))
    db_session.flush()
    db_session.expire_all()
    back = db_session.query(Skill).filter(Skill.slug == "all-decimal-small").one()
    assert back.id == special  # pre-fix: AttributeError 'int' has no 'replace'


def test_uuid_columns_render_text_affine_ddl_on_sqlite():
    """Guard the mechanism: on SQLite the DDL must be CHAR(32), never `UUID`.

    Literal ``UUID`` in a CREATE TABLE earns NUMERIC affinity on SQLite — the
    type-level root cause. On Postgres the DDL stays native ``UUID``.
    """
    ddl = str(CreateTable(Skill.__table__).compile(dialect=sqlite.dialect()))
    assert "id CHAR(32) NOT NULL" in ddl
    assert " UUID " not in ddl  # noqa: S105 - literal type name, not a secret

    pg_ddl = str(CreateTable(Skill.__table__).compile(dialect=postgresql.dialect()))
    assert "id UUID NOT NULL" in pg_ddl
