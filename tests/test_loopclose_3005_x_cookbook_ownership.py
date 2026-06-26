"""loopclose_3005 Phase X — cookbook ownership fixed for good.

RED-first tests (authored before the recipify.py fix) pinning the contract:

  1. A user-scoped MCP recipify call (ctx carries user_id, no user_id kwarg —
     exactly how server.py:_dispatch invokes it) creates a cookbook OWNED BY
     ctx.user_id and visible in that user's list_cookbooks. This is the
     reported bug: the orphan ("MCP Cookbook", bundle_owner=NULL) was
     invisible to every user forever.
  2. Fail-closed: a recipify create path that resolves no owner (no kwarg, no
     ctx.user_id, no target_cookbook_id) returns owner_required and writes
     ZERO rows — never an orphan.
  3. The DB CHECK invariant (is_base=true OR cookbook_owner IS NOT NULL) is
     exercised by the migration test (test_loopclose_3005_x_migration_psycopg2)
     against real Postgres; SQLite cannot enforce it.

These run on the SQLite fixture (fast). The migration/constraint proof is a
separate Postgres-gated test per alembic-postgres-only-sql-discipline.
"""
from __future__ import annotations

from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.mcp.tools import recipes_recipify
from app.models import Base, Bundle, User

_GOOD = """---
name: scrape-bot
description: A web-scraping skill that crawls and extracts structured data.
---
Scrape and ETL pipeline for analytics.
"""


@pytest.fixture()
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    sess = SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        Base.metadata.drop_all(bind=engine)


def _make_user(db: Session) -> User:
    user = User(
        id=uuid4(),
        display_name="Owner",
        email=f"{uuid4()}@example.com",
        subscription_tier="pro",
        subscription_status="active",
    )
    db.add(user)
    db.commit()
    return user


# ── Test 1: the actual bug — recipify via ctx owns the new cookbook ──────────


def test_recipify_via_ctx_creates_owned_cookbook(db_session):
    """server.py:_dispatch calls recipes_recipify(db, ctx=ctx, **args) — ctx
    carries the authenticated user_id, but NO user_id kwarg is passed. The
    new cookbook MUST be owned by ctx.user_id, not NULL."""
    user = _make_user(db_session)
    ctx = AuthContext(scope="user", user_id=user.id)

    out = recipes_recipify(
        db_session,
        slug="scrape-bot",
        content=_GOOD,
        ctx=ctx,  # no user_id kwarg, no target_cookbook_id — the exact bug call
    )
    assert "error" not in out, out
    from uuid import UUID as _UUID
    cb = db_session.query(Bundle).filter(Bundle.id == _UUID(out["cookbook_id"])).first()
    assert cb is not None
    assert cb.bundle_owner == user.id, (
        f"cookbook must be owned by ctx.user_id, got owner={cb.bundle_owner!r} "
        "(the orphan bug: bundle_owner=NULL → invisible to every user)"
    )
    # And it must be visible when filtering by that user's id (list_cookbooks semantics).
    visible = (
        db_session.query(Bundle)
        .filter(Bundle.bundle_owner == user.id)
        .all()
    )
    assert cb.id in [c.id for c in visible]


def test_recipify_via_ctx_reuses_existing_owned_cookbook(db_session):
    """A second recipify by the same ctx user reuses their first cookbook
    rather than spawning a second one."""
    user = _make_user(db_session)
    ctx = AuthContext(scope="user", user_id=user.id)

    out1 = recipes_recipify(db_session, slug="skill-one", content=_GOOD, ctx=ctx)
    out2 = recipes_recipify(
        db_session,
        slug="skill-two",
        content=_GOOD.replace("scrape-bot", "skill-two"),
        ctx=ctx,
    )
    assert out1["cookbook_id"] == out2["cookbook_id"]
    owned = (
        db_session.query(Bundle)
        .filter(Bundle.bundle_owner == user.id)
        .count()
    )
    assert owned == 1


# ── Test 2: fail closed — no resolvable owner never writes an orphan ─────────


def test_recipify_fails_closed_when_no_owner_resolves(db_session):
    """No user_id kwarg, ctx with user_id=None (or no ctx), no
    target_cookbook_id → owner_required, and ZERO cookbooks written."""
    before = db_session.query(Bundle).count()

    out = recipes_recipify(
        db_session,
        slug="orphan-attempt",
        content=_GOOD,
        ctx=AuthContext(scope="user", user_id=None),
    )
    assert out.get("code") == "owner_required", out
    after = db_session.query(Bundle).count()
    assert after == before, "fail-closed must write no rows (no orphan cookbook)"
    # And specifically: no owner-less non-base cookbook exists.
    orphans = (
        db_session.query(Bundle)
        .filter(Bundle.is_base == False, Bundle.bundle_owner.is_(None))  # noqa: E712
        .count()
    )
    assert orphans == 0


def test_recipify_with_explicit_user_id_kwarg_still_works(db_session):
    """The legacy explicit user_id kwarg path is preserved (no regression)."""
    user = _make_user(db_session)
    out = recipes_recipify(
        db_session,
        slug="legacy-kwarg",
        content=_GOOD,
        user_id=str(user.id),
    )
    assert "error" not in out, out
    from uuid import UUID as _UUID
    cb = db_session.query(Bundle).filter(Bundle.id == _UUID(out["cookbook_id"])).first()
    assert cb.bundle_owner == user.id
