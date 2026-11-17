"""t_555b26e2 — a UUID primary key must never come back from SQLite as a float.

The defect
----------
``app/models.py`` declared every uuid column as
``Column(sqlalchemy.dialects.postgresql.UUID(as_uuid=True))``. On Postgres that
renders ``UUID`` (a real type). On SQLite the postgresql-dialect type has no
SQLite compiler, so SQLAlchemy emits the bare word ``UUID`` as the type name —
and SQLite's affinity rules (https://www.sqlite.org/datatype3.html §3.1) only
pick TEXT when the declared name contains CHAR/CLOB/TEXT. ``UUID`` contains
none of INTEGER/CHAR/CLOB/TEXT/BLOB/REAL/FLOA/DOUB, so every uuid column got
**NUMERIC affinity**.

NUMERIC affinity means: when text is inserted into the column, SQLite converts
it to INTEGER or REAL if the text is a well-formed numeric literal. A uuid4 hex
string is 32 characters of ``[0-9a-f]``. Most of them are not numbers. But a
uuid4 whose hex is **all digits** (e.g. ``12345678123456781234567812345678``)
is a well-formed real literal too long for int64, so SQLite silently stores it
as a REAL — destroying the value — and a hex of the shape ``<digits>e<digits>``
with a large enough exponent becomes ``inf``::

    '12345678123456781234567812345678' -> typeof=real  value=1.2345678123456782e+31
    '1e999999999999999999999999999999' -> typeof=real  value=inf

Reading such a row back runs SQLAlchemy's Uuid result processor on a float:

    sqlalchemy/.../sqltypes.py: in process
        value = _python_UUID(value)
    >   hex = hex.replace('urn:', '').replace('uuid:', '')
    E   AttributeError: 'float' object has no attribute 'replace'

which is exactly the failure the ``random-order`` CI job printed on seed
3716405558 (``test_m1_private_bundle_meter.py::…test_over_limit_in_public_
bundles_still_allows_a_new_bundle[pro]``), inside SQLAlchemy's
``_deliver_insertmanyvalues_batches`` sentinel mapping.

Why it looked like an order-dependent flake
-------------------------------------------
It is not order-dependent and it is not seed-dependent. ``uuid.uuid4()`` reads
``os.urandom``; ``pytest-randomly`` does not seed it. The failure depends only
on whether some uuid minted during the run happened to be numeric-shaped — so
replaying the published seed reproduces nothing, which is how it survived as
"two order-dependent tests". P(a given uuid is numeric-shaped) is
``(10/16)^32 + 30·(1/16)·(10/16)^31 ≈ 1.2e-6`` (measured 2.0e-6 over 2M uuid4s),
so on a suite minting hundreds of thousands of ids it lands occasionally on
whichever run nobody can reproduce.

The fix
-------
``from sqlalchemy import Uuid as UUID``. ``sqlalchemy.Uuid`` is the 2.0
backend-agnostic type: it renders ``UUID`` on Postgres (verified byte-identical
to the old DDL — zero prod schema change) and ``CHAR(32)`` where there is no
native uuid type, which is TEXT affinity and stores the hex string untouched.

Tests below are three-layer on purpose: the structural gate fails loudly if a
new model reaches past ``app.models.UUID`` and reintroduces a NUMERIC-affinity
name; the behavioural test pins the exact value that broke CI; the executemany
test pins the code path (the insertmanyvalues sentinel) that crashed.

Provenance: first written for t_555b26e2 / PR #480 (2026-11-16), which went
unmerged; re-landed on fresh main by t_55218ee9 after the same defect fired
live on ``loop_manifests`` in a file-order local run (2026-11-17, parent
t_c75edf6b run R5) — a ``LoopManifest`` round-trip is pinned below for that
reason. Supersedes PR #461 and PR #480.
"""

from __future__ import annotations

import re
from uuid import UUID as PyUUID
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Bundle, LoopManifest, User

# The exact shape that killed the CI run: 32 decimal digits. Too long for int64,
# so SQLite's NUMERIC affinity stores it as a REAL and the Uuid result
# processor receives a float.
ALL_DIGIT_UUID = PyUUID("12345678123456781234567812345678")
# Same class, but the mantissa/exponent form that overflows to `inf`.
INF_SHAPE_UUID = PyUUID("1e999999999999999999999999999999")
# Control: one 'a' makes it a non-number, so even NUMERIC affinity leaves it alone.
CONTROL_UUID = PyUUID("a1234567812345678123456781234567")

_AFFINITY_SAFE = re.compile(r"CHAR|CLOB|TEXT|VARCHAR", re.IGNORECASE)


@pytest.fixture()
def engine():
    eng = sa.create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def db(engine):
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


# ── 1. structural: no uuid column may render a NUMERIC-affinity type name ──


def test_every_uuid_column_renders_a_text_affinity_type_on_sqlite():
    """Gate: catch a reintroduced NUMERIC-affinity uuid column at review time.

    This is the whole bug in one assertion. A uuid column whose SQLite type name
    is not TEXT-affinity is a column that can silently return a float.
    """
    offenders: list[str] = []
    for table in Base.metadata.sorted_tables:
        for col in table.columns:
            type_ = getattr(col, "type", None)
            if type_ is None:
                continue
            # Catch both spellings: the good one (sqlalchemy.Uuid) and the one
            # that caused this (postgresql.UUID, which is not a Uuid subclass).
            is_uuid = isinstance(type_, sa.Uuid) or type(type_).__name__ == "UUID"
            if not is_uuid:
                continue
            rendered = str(type_.compile(dialect=sqlite.dialect()))
            if not _AFFINITY_SAFE.search(rendered):
                offenders.append(f"{table.name}.{col.name} -> {rendered!r} ({type(type_).__name__})")
    assert not offenders, (
        "these uuid columns compile to a SQLite type name with NUMERIC affinity, "
        "so SQLite will coerce a numeric-shaped uuid hex into a float and "
        "sqlalchemy.Uuid will crash reading it back. Declare them with "
        f"app.models.UUID (= sqlalchemy.Uuid). Offenders: {offenders}"
    )


def test_uuid_columns_still_render_native_uuid_on_postgres():
    """The fix must not touch prod: Postgres DDL has to stay ``UUID``."""
    col = Bundle.__table__.columns["id"]
    assert str(col.type.compile(dialect=postgresql.dialect())) == "UUID"


def test_postgresql_dialect_uuid_type_is_not_imported_by_models():
    """Structural guard: ``postgresql.UUID`` has no SQLite compiler — that is the bug."""
    import inspect

    import app.models as models

    src = inspect.getsource(models)
    assert "dialects.postgresql import UUID" not in src, (
        "models.py must not import UUID from sqlalchemy.dialects.postgresql: that type "
        "has no SQLite compiler, so it emits the bare name UUID and SQLite gives the "
        "column NUMERIC affinity (see module docstring)."
    )
    assert "Uuid as UUID" in src, "models.py must alias the dialect-agnostic sqlalchemy.Uuid"


# ── 2. behavioural: the adversarial value round-trips ─────────────────────


@pytest.mark.parametrize(
    "bad_uuid",
    [ALL_DIGIT_UUID, INF_SHAPE_UUID, CONTROL_UUID],
    ids=["all-digits", "inf-shape", "control-has-hex-letter"],
)
def test_numeric_shaped_uuid_survives_a_sqlite_round_trip(engine, db, bad_uuid):
    """Insert a numeric-shaped uuid and read it back from the heap, not the identity map."""
    owner = User(id=uuid4(), display_name="u", email=f"{uuid4()}@x.example", subscription_tier="pro")
    db.add(owner)
    db.flush()
    owner_id = owner.id  # capture before expunge_all() detaches the instance

    bundle = Bundle(
        id=bad_uuid,
        name="affinity probe",
        bundle_owner=owner.id,
        slug=f"probe-{uuid4().hex[:12]}",
        visibility="public",
    )
    db.add(bundle)
    db.commit()

    # What SQLite actually stored. Pre-fix this is 'real' for the two adversarial
    # cases — the value is already destroyed at this point.
    stored_type = (
        db.connection()
        .exec_driver_sql("SELECT typeof(id) FROM bundles WHERE slug = ?", (bundle.slug,))
        .scalar_one()
    )
    assert stored_type == "text", (
        f"uuid {bad_uuid} was stored as {stored_type!r}: the column has NUMERIC affinity"
    )

    db.expunge_all()  # force a real SELECT; the identity map would hide the bug
    fetched = db.get(Bundle, bad_uuid)
    assert fetched is not None, f"row with uuid {bad_uuid} is unreadable"
    assert fetched.id == bad_uuid
    assert fetched.bundle_owner == owner_id


def test_executemany_flush_of_numeric_shaped_uuids(engine, db):
    """Pin the code path CI crashed on: >=2 rows in one flush -> insertmanyvalues.

    The traceback was inside ``_deliver_insertmanyvalues_batches`` building
    ``rows_by_sentinel`` — SQLAlchemy maps batched rows back by processing the
    sentinel (the uuid PK) column, so a float sentinel raises
    ``AttributeError: 'float' object has no attribute 'replace'`` during flush(),
    not during a later SELECT.
    """
    owner = User(id=uuid4(), display_name="u", email=f"{uuid4()}@x.example", subscription_tier="pro")
    db.add(owner)
    db.flush()

    seen: dict[str, object] = {}

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        if executemany and "INSERT INTO bundles" in statement:
            seen["executemany"] = True

    ids = [ALL_DIGIT_UUID, INF_SHAPE_UUID, uuid4(), uuid4(), uuid4()]
    for i, rid in enumerate(ids):
        db.add(
            Bundle(
                id=rid,
                name=f"batch-{i}",
                bundle_owner=owner.id,
                slug=f"batch-{i}-{uuid4().hex[:12]}",
                visibility="public",
            )
        )
    db.flush()  # <- pre-fix: AttributeError: 'float' object has no attribute 'replace'

    assert seen.get("executemany"), "test lost its premise: the insert was not batched"
    db.expunge_all()
    assert {b.id for b in db.query(Bundle).filter(Bundle.id.in_(ids)).all()} == set(ids)


# ── 3. the table that fired live: loop_manifests (t_c75edf6b run R5) ────────


@pytest.mark.parametrize(
    "bad_uuid",
    [ALL_DIGIT_UUID, INF_SHAPE_UUID, CONTROL_UUID],
    ids=["all-digits", "inf-shape", "control-has-hex-letter"],
)
def test_loop_manifest_numeric_shaped_uuid_survives_round_trip(engine, db, bad_uuid):
    """``loop_manifests`` fired this defect live on 2026-11-17 (parent t_c75edf6b).

    A file-order full-suite run crashed in
    ``tests/test_fleetos_A_placements.py::test_reconcile_precheck_dispatches_through_server``
    with ``hex = inf`` — a float reaching ``uuid.UUID()`` via this table's
    ``id``/``owner_user_id``/``org_id`` columns. Pin all three uuid columns of
    that table: the PK, the nullable owner, and the FK-carrying org_id.
    """
    owner = User(id=uuid4(), display_name="lm-owner", email=f"{uuid4()}@x.example", subscription_tier="pro")
    db.add(owner)
    db.flush()
    owner_id = owner.id  # capture before expunge_all() detaches the instance

    manifest = LoopManifest(
        id=bad_uuid,
        loop_id=f"probe-{bad_uuid.hex[:8]}",
        schedule="30m",
        prompt="affinity probe",
        owner_user_id=owner.id,
        # org_id stays NULL: the FK target orgs.id is itself a uuid column, and
        # the point here is the manifest's own three uuid columns, not the join.
    )
    db.add(manifest)
    db.commit()

    stored_type = (
        db.connection()
        .exec_driver_sql(
            "SELECT typeof(id), typeof(owner_user_id) FROM loop_manifests WHERE loop_id = ?",
            (manifest.loop_id,),
        )
        .one()
    )
    assert stored_type == ("text", "text"), (
        f"loop_manifests uuid columns stored as {stored_type!r}: NUMERIC affinity is back"
    )

    db.expunge_all()
    fetched = db.get(LoopManifest, bad_uuid)
    assert fetched is not None, f"loop_manifest row with uuid {bad_uuid} is unreadable"
    assert fetched.id == bad_uuid
    assert fetched.owner_user_id == owner_id
    assert fetched.org_id is None
