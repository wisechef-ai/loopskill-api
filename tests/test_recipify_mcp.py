"""recipes_recipify MCP tool — Phase G round-trip."""
from __future__ import annotations

from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.mcp.tools import recipes_recipify
from app.models import Base, Bundle, BundleSkill, Skill, User


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


def test_mcp_tool_round_trip(db_session):
    user = User(id=uuid4(), display_name="T", email="m@x.example",
                subscription_tier="cook", subscription_status="active")
    cb = Bundle(id=uuid4(), name="MCP CB", bundle_owner=user.id)
    db_session.add_all([user, cb])
    db_session.commit()

    out = recipes_recipify(
        db_session,
        slug="scrape-bot",
        content=_GOOD,
        target_cookbook_id=str(cb.id),
    )
    assert "error" not in out, out
    assert out["slug"] == "scrape-bot"
    assert out["cookbook_id"] == str(cb.id)
    assert out["category"] == "data"
    assert out["status"] == "created"

    skill = db_session.query(Skill).filter(Skill.slug == "scrape-bot").first()
    assert skill is not None
    cs = db_session.query(BundleSkill).filter(
        BundleSkill.bundle_id == cb.id,
        BundleSkill.skill_id == skill.id,
    ).first()
    assert cs is not None


def test_mcp_tool_no_longer_returns_not_implemented(db_session):
    user = User(id=uuid4(), display_name="T", email="m2@x.example",
                subscription_tier="cook", subscription_status="active")
    cb = Bundle(id=uuid4(), name="MCP CB", bundle_owner=user.id)
    db_session.add_all([user, cb])
    db_session.commit()

    out = recipes_recipify(
        db_session,
        slug="another-slug",
        content=_GOOD,
        target_cookbook_id=str(cb.id),
    )
    assert out.get("error") != "not_implemented"
    assert out.get("phase") != "G"
    assert "slug" in out


def test_mcp_tool_rejects_bad_frontmatter(db_session):
    user = User(id=uuid4(), display_name="T", email="m3@x.example",
                subscription_tier="cook", subscription_status="active")
    cb = Bundle(id=uuid4(), name="MCP CB", bundle_owner=user.id)
    db_session.add_all([user, cb])
    db_session.commit()

    out = recipes_recipify(
        db_session,
        slug="ok-slug",
        content="no frontmatter here",
        target_cookbook_id=str(cb.id),
    )
    assert out.get("code") == "invalid_frontmatter"
