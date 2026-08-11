"""bundles0811 P3.6 gate closure — bulk personality add/remove.

Closes gate item (2): "Personalities and MCPs manageable through the same
bundle primitive, OR an explicit written out-of-scope decision" — see
docs/decisions/2026-08-11-bundles0811-p36-personalities-mcp-scope.md.

Covers:
  - bulk add of many personalities in ONE call (mirrors add_skills_bulk)
  - partial-failure: an unknown slug does not lose the good items
  - idempotency: re-adding an already-declared personality reports
    reactivated=True and does not duplicate the row
  - bulk remove, including unknown-slug no-op idempotency
  - a >=25-personality TIMED demo, matching the skill acceptance-gate shape
"""

from __future__ import annotations

import time
import uuid
from typing import Generator

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import patch

from app.models import Base, Bundle, BundlePersonality, Personality, User


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


def _mk_user(db, tier="pro_plus"):
    u = User(
        id=uuid.uuid4(),
        github_id=int(uuid.uuid4().int) % 1_000_000_000,
        email=f"u-{uuid.uuid4().hex[:6]}@t.io",
        display_name="u",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.commit()
    return u


def _mk_cookbook(db, owner, name="CB"):
    cb = Bundle(id=uuid.uuid4(), name=name, bundle_owner=owner.id)
    db.add(cb)
    db.commit()
    return cb


def _mk_personality(db, slug):
    p = Personality(
        id=uuid.uuid4(), slug=slug, title=slug, is_public=True, system_prompt="you are a test persona"
    )
    db.add(p)
    db.commit()
    return p


class _State:
    api_key_id = None


class _Req:
    client = None
    state = _State()
    method = "POST"
    url = type("U", (), {"path": "/api/cookbooks/x/personalities/bulk"})()


def _ctx(owner, tier="pro_plus"):
    class _Ctx:
        pass

    c = _Ctx()
    c.is_master = False
    c.user_id = owner.id
    c.tier = tier
    c.cbt_cookbook_id = None
    c.org_id = None
    return c


def _bypass_scope_guard():
    """Bypass ONLY the cbt-scope guard (AGENTS.md mandatory call) — leaves
    `_resolve_owned_cookbook` REAL so authz tests exercise the actual
    ownership predicate, not a stub."""
    from app import bundle_routes

    return patch.object(bundle_routes, "_enforce_cbt_scope_for_cookbook_route", return_value=None)


# ── bulk add ─────────────────────────────────────────────────────────────


def test_bulk_add_personalities_one_operation(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    slugs = [f"persona-{i}" for i in range(10)]
    for slug in slugs:
        _mk_personality(db, slug)
    items = [{"slug": s} for s in slugs]

    with _bypass_scope_guard():
        out = bundle_routes.add_personalities_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkPersonalityAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert out["total"] == 10
    assert out["added"] == 10
    assert out["failed"] == 0
    rows = db.query(BundlePersonality).filter(BundlePersonality.bundle_id == cb.id).all()
    assert len(rows) == 10


def test_bulk_add_personalities_partial_failure_bad_slug_does_not_lose_good_items(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_personality(db, "good-persona-1")
    _mk_personality(db, "good-persona-2")
    items = [
        {"slug": "good-persona-1"},
        {"slug": "does-not-exist-anywhere"},
        {"slug": "good-persona-2"},
    ]

    with _bypass_scope_guard():
        out = bundle_routes.add_personalities_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkPersonalityAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert out["total"] == 3
    assert out["added"] == 2
    assert out["failed"] == 1
    by_identity = {r["identity"]: r for r in out["results"]}
    assert by_identity["good-persona-1"]["ok"] is True
    assert by_identity["good-persona-2"]["ok"] is True
    assert by_identity["does-not-exist-anywhere"]["ok"] is False
    assert by_identity["does-not-exist-anywhere"]["error"] == "personality_not_found"

    rows = db.query(BundlePersonality).filter(BundlePersonality.bundle_id == cb.id).all()
    assert len(rows) == 2


def test_bulk_add_personalities_repeated_is_idempotent(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_personality(db, "persona-x")
    items = [{"slug": "persona-x"}]

    with _bypass_scope_guard():
        out1 = bundle_routes.add_personalities_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkPersonalityAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )
        out2 = bundle_routes.add_personalities_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkPersonalityAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert out1["added"] == 1
    assert out2["added"] == 0
    assert out2["reactivated"] == 1
    rows = db.query(BundlePersonality).filter(BundlePersonality.bundle_id == cb.id).all()
    assert len(rows) == 1


def test_bulk_add_personalities_empty_batch_is_422(db):
    from app import bundle_routes
    from fastapi import HTTPException

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)

    with _bypass_scope_guard(), pytest.raises(HTTPException) as exc_info:
        bundle_routes.add_personalities_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkPersonalityAddIn(items=[]),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )
    assert exc_info.value.status_code == 422


def test_bulk_add_personalities_non_owner_is_rejected(db):
    from app import bundle_routes
    from fastapi import HTTPException

    owner = _mk_user(db)
    intruder = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    _mk_personality(db, "persona-owned")

    with _bypass_scope_guard(), pytest.raises(HTTPException) as exc_info:
        bundle_routes.add_personalities_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkPersonalityAddIn(items=[{"slug": "persona-owned"}]),
            request=_Req(),
            db=db,
            ctx=_ctx(intruder),
        )
    assert exc_info.value.status_code == 404


# ── bulk remove ──────────────────────────────────────────────────────────


def test_bulk_remove_personalities(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    p1 = _mk_personality(db, "rm-persona-1")
    p2 = _mk_personality(db, "rm-persona-2")
    db.add(BundlePersonality(bundle_id=cb.id, personality_id=p1.id))
    db.add(BundlePersonality(bundle_id=cb.id, personality_id=p2.id))
    db.commit()

    with _bypass_scope_guard():
        out = bundle_routes.remove_personalities_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkPersonalityRemoveIn(
                items=[{"slug": "rm-persona-1"}, {"slug": "rm-persona-2"}]
            ),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert out["total"] == 2
    assert out["removed"] == 2
    rows = db.query(BundlePersonality).filter(BundlePersonality.bundle_id == cb.id).all()
    assert len(rows) == 0


def test_bulk_remove_personalities_unknown_slug_is_idempotent_no_op(db):
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)

    with _bypass_scope_guard():
        out = bundle_routes.remove_personalities_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkPersonalityRemoveIn(items=[{"slug": "never-added"}]),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )

    assert out["total"] == 1
    assert out["removed"] == 1
    assert out["failed"] == 0


# ── TIMED acceptance-gate demo: >=25 personalities in ONE operation ────────


def test_timed_bulk_add_25_personality_bundle_acceptance_gate(db, capsys):
    """Same acceptance-gate shape as the skill demo
    (test_timed_bulk_create_25_skill_bundle_acceptance_gate), applied to the
    personality bulk endpoint this PR adds — proof the bulk-op verb
    generalizes beyond skills without a schema change."""
    from app import bundle_routes

    owner = _mk_user(db)
    cb = _mk_cookbook(db, owner)
    slugs = [f"timed-persona-{i}" for i in range(30)]
    for slug in slugs:
        _mk_personality(db, slug)
    items = [{"slug": s} for s in slugs]

    with _bypass_scope_guard():
        start = time.perf_counter()
        out = bundle_routes.add_personalities_bulk(
            cookbook_id=str(cb.id),
            body=bundle_routes.BulkPersonalityAddIn(items=items),
            request=_Req(),
            db=db,
            ctx=_ctx(owner),
        )
        elapsed = time.perf_counter() - start

    assert out["total"] == 30
    assert out["added"] == 30
    print(
        f"\n[P3.6 gate-closure demo] bulk-added {out['total']} personalities in ONE "
        f"operation: {elapsed * 1000:.2f}ms"
    )
    assert elapsed < 5.0
