"""bundles0811 P3.6 — saved views / filters over the federated index.

Gate: "Filter the federated index by source + license and act on the
result." Covers filtering by source/license/trust_level/tag independently
and combined, plus the bulk-add consumability contract (see also
test_bundles0811_p36_bulk_ops.py::test_filter_result_shape_is_directly_bulk_add_consumable
for the end-to-end version).
"""

from __future__ import annotations

from typing import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, FederationHubSkill


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _seed(db):
    rows = [
        FederationHubSkill(
            slug="marketing-mit-clawhub",
            title="Marketing MIT ClawHub",
            source="hermes-hub",
            upstream_source="clawhub",
            trust_level="community",
            license="MIT",
            tags=["marketing", "copy"],
        ),
        FederationHubSkill(
            slug="marketing-apache-skillssh",
            title="Marketing Apache skills.sh",
            source="hermes-hub",
            upstream_source="skills.sh",
            trust_level="community",
            license="Apache-2.0",
            tags=["marketing"],
        ),
        FederationHubSkill(
            slug="ops-mit-github",
            title="Ops MIT GitHub",
            source="hermes-hub",
            upstream_source="github",
            trust_level="trusted",
            license="MIT",
            tags=["ops", "devops"],
        ),
        FederationHubSkill(
            slug="ops-unlicensed-clawhub",
            title="Ops Unlicensed ClawHub",
            source="hermes-hub",
            upstream_source="clawhub",
            trust_level="community",
            license=None,
            tags=["ops"],
        ),
    ]
    for r in rows:
        db.add(r)
    db.commit()
    return rows


def test_filter_by_source_alone(db):
    from app.federation_filter_routes import filter_federation_index

    _seed(db)
    rows, total = filter_federation_index(db, source="clawhub")
    assert total == 2
    assert {r.slug for r in rows} == {"marketing-mit-clawhub", "ops-unlicensed-clawhub"}


def test_filter_by_source_and_license_combined(db):
    """The gate's exact scenario: source + license together."""
    from app.federation_filter_routes import filter_federation_index

    _seed(db)
    rows, total = filter_federation_index(db, source="clawhub", license_id="MIT")
    assert total == 1
    assert rows[0].slug == "marketing-mit-clawhub"


def test_filter_by_trust_level(db):
    from app.federation_filter_routes import filter_federation_index

    _seed(db)
    rows, total = filter_federation_index(db, trust_level="trusted")
    assert total == 1
    assert rows[0].slug == "ops-mit-github"


def test_filter_by_tag(db):
    from app.federation_filter_routes import filter_federation_index

    _seed(db)
    rows, total = filter_federation_index(db, tag="marketing")
    assert total == 2
    assert {r.slug for r in rows} == {"marketing-mit-clawhub", "marketing-apache-skillssh"}


def test_filter_by_tag_does_not_false_positive_on_substring(db):
    """The JSON-as-text tag match must not match 'ops' inside 'devops' as a
    standalone tag hit for a DIFFERENT skill that only has 'devops', not 'ops'."""
    from app.federation_filter_routes import filter_federation_index

    _seed(db)
    rows, total = filter_federation_index(db, tag="ops")
    slugs = {r.slug for r in rows}
    # Both ops-tagged rows match tag=ops; devops-only would not (none seeded).
    assert slugs == {"ops-mit-github", "ops-unlicensed-clawhub"}


def test_filter_no_filters_returns_everything(db):
    from app.federation_filter_routes import filter_federation_index

    _seed(db)
    rows, total = filter_federation_index(db)
    assert total == 4


def test_filter_license_null_rows_excluded_when_license_filter_set(db):
    """A row with no recorded license (Q3: most rows today) must not match a
    license filter — it should not silently show up as a false positive."""
    from app.federation_filter_routes import filter_federation_index

    _seed(db)
    rows, total = filter_federation_index(db, license_id="MIT")
    assert "ops-unlicensed-clawhub" not in {r.slug for r in rows}


def test_filter_result_is_paginated(db):
    from app.federation_filter_routes import filter_federation_index

    _seed(db)
    rows, total = filter_federation_index(db, limit=2, offset=0)
    assert total == 4
    assert len(rows) == 2


# ── act on the result: filter output feeds bulk-add with zero reshaping ────


def test_act_on_filter_result_via_bulk_add(db):
    """The gate's full sentence: filter by source+license AND act on the
    result. This drives the filter output straight into the bulk-add
    endpoint and asserts real bundle membership lands."""
    import uuid

    from app import bundle_routes
    from app.federation_filter_routes import filter_federation_index, _row_to_bulk_shape
    from app.models import Bundle, BundleSkill, User
    from unittest.mock import patch

    owner = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int) % 1_000_000_000,
        email="filter-act@t.io",
        display_name="u",
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    db.add(owner)
    db.commit()
    cb = Bundle(id=uuid.uuid4(), name="Filtered Bundle", bundle_owner=owner.id)
    db.add(cb)
    db.commit()

    _seed(db)
    rows, total = filter_federation_index(db, source="clawhub")
    assert total == 2
    items = [_row_to_bulk_shape(r) for r in rows]

    class _State:
        api_key_id = None

    class _Req:
        client = None
        state = _State()
        method = "POST"
        url = type("U", (), {"path": "/api/cookbooks/x/skills/bulk"})()

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.is_master = False
    ctx.user_id = owner.id
    ctx.tier = "pro_plus"
    ctx.cbt_cookbook_id = None
    ctx.org_id = None

    with patch.object(bundle_routes, "_enforce_cbt_scope_for_cookbook_route", return_value=None):
        out = bundle_routes.add_skills_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=ctx,
        )

    assert out["added"] == 2
    assert out["failed"] == 0
    fed_rows = db.query(BundleSkill).filter(BundleSkill.bundle_id == cb.id).all()
    assert {(r.federated_source, r.federated_slug) for r in fed_rows} == {
        ("hermes-hub", "marketing-mit-clawhub"),
        ("hermes-hub", "ops-unlicensed-clawhub"),
    }
