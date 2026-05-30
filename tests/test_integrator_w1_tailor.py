"""Tests for integrator_2905 W1: tailor MCP tools + Pro-tier fork gate.

Covers:
1. _is_pro_tier: Pro and above accepted
2. _is_pro_tier: Free/None rejected
3. recipes_tailor: happy-path fork creation
4. recipes_tailor: idempotent (existing fork returned)
5. recipes_tailor: source skill not found
6. recipes_tailor: master key rejected (no user_id)
7. recipes_tailor: Pro+ still works
8. recipes_fork_list: returns user's forks
9. recipes_fork_list: master key returns empty
10. MCP dispatch: tools registered in definitions
11. MCP dispatch: tailor dispatched via call_tool_sync
12. MCP dispatch: fork_list dispatched via call_tool_sync
"""

from __future__ import annotations

from typing import Generator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.models import Base, Skill, User


# ── DB fixtures (self-contained like test_forks_routes.py) ────────────────


@pytest.fixture(scope="module")
def _engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(_engine) -> Generator[Session, None, None]:
    connection = _engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_user(db: Session, tier: str) -> User:
    uid = uuid4()
    user = User(
        id=uid,
        display_name="Tester",
        email=f"{uid}@test.example",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(user)
    db.flush()
    return user


def _make_skill(db: Session, slug: str = "source-skill") -> Skill:
    s = Skill(
        id=uuid4(),
        slug=slug,
        title="Source Skill",
        is_public=True,
    )
    db.add(s)
    db.flush()
    return s


# ── Tier label tests ─────────────────────────────────────────────────────


class TestTierLabels:
    def test_pro_tier_accepted(self) -> None:
        from app.tier_labels import _is_pro_tier

        assert _is_pro_tier("pro") is True
        assert _is_pro_tier("pro_plus") is True
        assert _is_pro_tier("cook") is True  # legacy alias → pro
        assert _is_pro_tier("operator") is True  # legacy alias → pro_plus
        assert _is_pro_tier("studio") is True  # legacy alias → pro_plus

    def test_free_tier_rejected(self) -> None:
        from app.tier_labels import _is_pro_tier

        assert _is_pro_tier("free") is False
        assert _is_pro_tier(None) is False
        assert _is_pro_tier("") is False


# ── MCP: recipes_tailor ──────────────────────────────────────────────────


class TestRecipesTailor:
    def test_happy_path_fork(self, db: Session) -> None:
        user = _make_user(db, "pro")
        skill = _make_skill(db, "tailor-skill-1")
        db.commit()

        from app.mcp.tools.tailor import recipes_tailor

        ctx = AuthContext(scope="user", user_id=user.id, tier="pro")
        result = recipes_tailor(
            db, source_slug=skill.slug, name="My Tailored Fork", ctx=ctx,
        )
        assert result["status"] == "forked"
        assert result["source_slug"] == skill.slug
        assert "fork_id" in result
        assert "fork_slug" in result

    def test_idempotent_existing_fork(self, db: Session) -> None:
        user = _make_user(db, "pro")
        skill = _make_skill(db, "tailor-skill-2")
        db.commit()

        from app.mcp.tools.tailor import recipes_tailor

        ctx = AuthContext(scope="user", user_id=user.id, tier="pro")

        r1 = recipes_tailor(db, source_slug=skill.slug, name="My Fork", ctx=ctx)
        assert r1["status"] == "forked"

        r2 = recipes_tailor(db, source_slug=skill.slug, name="My Fork", ctx=ctx)
        assert r2["status"] == "existing"
        assert r2["fork_id"] == r1["fork_id"]

    def test_source_not_found(self, db: Session) -> None:
        user = _make_user(db, "pro")
        db.commit()

        from app.mcp.tools.tailor import recipes_tailor

        ctx = AuthContext(scope="user", user_id=user.id, tier="pro")
        result = recipes_tailor(db, source_slug="nonexistent-xyz", name="Test", ctx=ctx)
        assert result.get("error") == "source_not_found"

    def test_master_key_rejected(self, db: Session) -> None:
        skill = _make_skill(db, "tailor-skill-3")
        db.commit()

        from app.mcp.tools.tailor import recipes_tailor

        ctx = AuthContext(scope="master")
        result = recipes_tailor(db, source_slug=skill.slug, name="Master Fork", ctx=ctx)
        assert result.get("error") == "auth_required"

    def test_pro_plus_still_works(self, db: Session) -> None:
        user = _make_user(db, "pro_plus")
        skill = _make_skill(db, "tailor-skill-4")
        db.commit()

        from app.mcp.tools.tailor import recipes_tailor

        ctx = AuthContext(scope="user", user_id=user.id, tier="pro_plus")
        result = recipes_tailor(db, source_slug=skill.slug, name="Pro+ Fork", ctx=ctx)
        assert result["status"] == "forked"


# ── MCP: recipes_fork_list ───────────────────────────────────────────────


class TestRecipesForkList:
    def test_returns_user_forks(self, db: Session) -> None:
        user = _make_user(db, "pro")
        skill = _make_skill(db, "list-skill-1")
        db.commit()

        from app.mcp.tools.tailor import recipes_fork_list, recipes_tailor

        ctx = AuthContext(scope="user", user_id=user.id, tier="pro")
        recipes_tailor(db, source_slug=skill.slug, name="My Fork", ctx=ctx)

        result = recipes_fork_list(db, ctx=ctx)
        assert "forks" in result
        assert len(result["forks"]) == 1
        assert result["forks"][0]["source_slug"] == skill.slug

    def test_master_key_returns_empty(self, db: Session) -> None:
        from app.mcp.tools.tailor import recipes_fork_list

        ctx = AuthContext(scope="master")
        result = recipes_fork_list(db, ctx=ctx)
        assert result == {"forks": []}

    def test_none_ctx_returns_empty(self, db: Session) -> None:
        from app.mcp.tools.tailor import recipes_fork_list

        result = recipes_fork_list(db, ctx=None)
        assert result == {"forks": []}


# ── MCP dispatch integration ─────────────────────────────────────────────


class TestMCPDispatchIntegration:
    def test_tools_registered_in_definitions(self) -> None:
        from app.mcp.registry import _tool_definitions

        names = [t.name for t in _tool_definitions()]
        assert "recipes_tailor" in names, f"recipes_tailor not in {names}"
        assert "recipes_fork_list" in names, f"recipes_fork_list not in {names}"

    def test_tailor_dispatched_via_call_tool_sync(self, db: Session) -> None:
        user = _make_user(db, "pro")
        skill = _make_skill(db, "dispatch-skill-1")
        db.commit()

        from app.mcp.server import call_tool_sync

        ctx = AuthContext(scope="user", user_id=user.id, tier="pro")
        caller = {
            "scope": "user",
            "user_id": user.id,
            "api_key_id": None,
            "auth_ctx": ctx,
        }
        payload = call_tool_sync(
            "recipes_tailor",
            {"source_slug": skill.slug, "name": "Dispatch Test Fork"},
            caller=caller,
            db=db,
        )
        # call_tool_sync injects cookbook_status; verify the core result
        assert payload.get("status") in ("forked", "existing"), f"Unexpected: {payload}"

    def test_fork_list_dispatched_via_call_tool_sync(self, db: Session) -> None:
        user = _make_user(db, "pro")
        db.commit()

        from app.mcp.server import call_tool_sync

        ctx = AuthContext(scope="user", user_id=user.id, tier="pro")
        caller = {
            "scope": "user",
            "user_id": user.id,
            "api_key_id": None,
            "auth_ctx": ctx,
        }
        payload = call_tool_sync(
            "recipes_fork_list",
            {},
            caller=caller,
            db=db,
        )
        assert "forks" in payload
