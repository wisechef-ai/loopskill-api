"""Issue #420 — a UUID primary key must round-trip through SQLite as a UUID.

``sqlalchemy.dialects.postgresql.UUID`` has no SQLite-specific DDL, so it
rendered as a column of declared type ``UUID``. SQLite derives column affinity
from the declared type name, and ``UUID`` contains none of INT / CHAR / CLOB /
TEXT / BLOB / REAL / FLOA / DOUB, so it gets NUMERIC affinity. SQLAlchemy binds
a UUID on SQLite as 32 hex chars; any hex string that happens to be a
well-formed number (all digits, or digits + one ``e`` + digits) was silently
coerced by SQLite to an INTEGER or REAL — a huge exponent overflows to
``inf``. Reading it back then crashed ``uuid.UUID(float('inf'))`` with
``'float' object has no attribute 'replace'``. A random uuid4 hits this about
once in 10^5, which is the order-looking CI flake from #420.

The tests use fixed UUIDs, so they are deterministic rather than probabilistic.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, User

# 32 hex chars that SQLite would parse as a number under NUMERIC affinity.
NUMERIC_LOOKING = [
    # digits + one 'e' + digits: parsed as REAL, exponent overflows to inf
    uuid.UUID("12345678901234567890123456e78901"),
    # digits + one 'e' with a small exponent: parsed as a finite REAL (value changes)
    uuid.UUID("00000000000000000000000000e00001"),
    # all digits: parsed as INTEGER/REAL (leading zeros lost)
    uuid.UUID("00000000000000000000000000000001"),
    uuid.UUID("12345678901234567890123456789012"),
]


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()
        Base.metadata.drop_all(bind=engine)


@pytest.mark.parametrize("value", NUMERIC_LOOKING, ids=lambda u: u.hex)
def test_numeric_looking_uuid_pk_round_trips(session, value):
    user = User(
        id=value,
        display_name="x",
        subscription_tier="free",
        subscription_status="active",
    )
    session.add(user)
    session.commit()
    session.expire_all()  # force a real SELECT, as the flaking test did
    assert user.id == value
    assert isinstance(user.id, uuid.UUID)


def test_uuid_columns_use_text_affinity_on_sqlite(session):
    """The structural cause: no UUID column may be declared NUMERIC-affinity."""
    rows = session.execute(text("PRAGMA table_info(users)")).fetchall()
    declared = {r[1]: r[2] for r in rows}
    assert "CHAR" in declared["id"].upper(), declared["id"]


def test_all_uuid_columns_in_schema_have_text_affinity(session):
    bad = []
    for table in Base.metadata.sorted_tables:
        rows = session.execute(text(f'PRAGMA table_info("{table.name}")')).fetchall()
        for _cid, name, decl, *_ in rows:
            col = table.c[name]
            if col.type.__class__.__name__ == "UUID" and "CHAR" not in decl.upper():
                bad.append(f"{table.name}.{name}: {decl}")
    assert not bad, bad
