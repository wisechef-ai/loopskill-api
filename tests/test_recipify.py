"""Phase B TDD tests for write_cookbook_skill tier/is_public/creator_id kwargs.

These tests are written RED-first (before patching app/recipify.py).
Expected to FAIL against the baseline implementation.
"""
from __future__ import annotations

from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.models import Base, Bundle, Creator, Skill, User
from app.recipify import write_cookbook_skill

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
    _SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    sess = _SessionLocal()
    try:
        yield sess
    finally:
        sess.close()
        Base.metadata.drop_all(bind=engine)


def _make_cookbook(db: Session) -> Bundle:
    """Create and persist a minimal cookbook."""
    cb = Bundle(id=uuid4(), name="Test CB", bundle_owner=None)
    db.add(cb)
    db.commit()
    return cb


# ── Test 1 ───────────────────────────────────────────────────────────────────


def test_tier_kwarg_honored_on_create_and_update(db_session):
    """Passing tier='pro' must land on the Skill row for both create AND update paths."""
    cb = _make_cookbook(db_session)

    # ── CREATE path ──────────────────────────────────────────────────────────
    cs, status = write_cookbook_skill(
        slug="scrape-bot",
        content=_GOOD,
        target_cookbook_id=cb.id,
        visibility="private",
        db=db_session,
        tier="pro",
    )
    assert status == "created"
    skill = db_session.query(Skill).filter(Skill.slug == "scrape-bot").first()
    assert skill is not None
    assert skill.tier == "pro", f"Expected tier='pro' on create, got {skill.tier!r}"

    # ── UPDATE path (same slug again with a different tier) ───────────────────
    cs2, status2 = write_cookbook_skill(
        slug="scrape-bot",
        content=_GOOD,
        target_cookbook_id=cb.id,
        visibility="private",
        db=db_session,
        tier="operator",
    )
    assert status2 == "updated"
    db_session.refresh(skill)
    assert skill.tier == "operator", (
        f"Expected tier='operator' after update, got {skill.tier!r}"
    )


# ── Test 2 ───────────────────────────────────────────────────────────────────


def test_creator_id_set_from_ctx(db_session):
    """When ctx.user_id is set, write_cookbook_skill must resolve/create a Creator
    row and set skill.creator_id correctly (auto-creation path)."""
    cb = _make_cookbook(db_session)
    user_id = uuid4()
    user = User(
        id=user_id,
        display_name="Alice",
        email=f"{user_id}@example.com",
        subscription_tier="pro",
        subscription_status="active",
    )
    db_session.add(user)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_id)

    cs, status = write_cookbook_skill(
        slug="ctx-skill",
        content=_GOOD,
        target_cookbook_id=cb.id,
        visibility="private",
        db=db_session,
        ctx=ctx,
    )
    assert status == "created"

    skill = db_session.query(Skill).filter(Skill.slug == "ctx-skill").first()
    assert skill is not None
    assert skill.creator_id is not None, "creator_id must be set when ctx.user_id is provided"

    creator = db_session.query(Creator).filter(Creator.id == skill.creator_id).first()
    assert creator is not None, "Creator row must exist"
    assert creator.user_id == user_id, (
        f"Creator.user_id must match ctx.user_id, got {creator.user_id!r}"
    )


# ── Test 3 ───────────────────────────────────────────────────────────────────


def test_is_public_kwarg_independent_of_visibility(db_session):
    """Explicit is_public=True must override visibility='private' on the Skill row.
    Also: is_public=False must override visibility='public_pending_review'."""
    cb = _make_cookbook(db_session)

    # Case A: is_public=True wins over visibility='private'
    cs, status = write_cookbook_skill(
        slug="pub-skill",
        content=_GOOD,
        target_cookbook_id=cb.id,
        visibility="private",
        db=db_session,
        is_public=True,
    )
    assert status == "created"
    skill = db_session.query(Skill).filter(Skill.slug == "pub-skill").first()
    assert skill is not None
    assert skill.is_public is True, (
        f"Expected is_public=True when kwarg overrides visibility='private', got {skill.is_public!r}"
    )

    # Case B: is_public=False wins over visibility='public_pending_review'
    cb2 = _make_cookbook(db_session)
    cs2, status2 = write_cookbook_skill(
        slug="priv-skill",
        content=_GOOD,
        target_cookbook_id=cb2.id,
        visibility="public_pending_review",
        db=db_session,
        is_public=False,
    )
    assert status2 == "created"
    skill2 = db_session.query(Skill).filter(Skill.slug == "priv-skill").first()
    assert skill2 is not None
    assert skill2.is_public is False, (
        f"Expected is_public=False when kwarg overrides visibility='public_pending_review', "
        f"got {skill2.is_public!r}"
    )
