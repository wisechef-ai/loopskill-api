"""loopclose_3005 Phase I — cookbook handoff acceptance tests.

TDD-first: written BEFORE the implementation.

Contract:
  recipes_cookbook_handoff(db, *, ctx, cookbook_id, new_owner_user_id=None,
                           new_owner_email=None, mode='transfer'|'fork')

  transfer:
    - cookbook.bundle_owner → new_owner.id (in-place)
    - new_owner sees it via list_cookbooks
    - old_owner no longer sees it (unless also new owner, impossible)
    - Returns {status: 'transferred', cookbook_id: …, new_owner_user_id: …}

  fork:
    - creates NEW cookbook owned by new_owner
    - new cookbook.parent_cookbook_id == source.id
    - new cookbook.synced_from_cookbook_id == source.id
    - all custom-added CookbookSkill rows are copied
    - non-custom-added rows (forked/overridden/disabled) are NOT copied
    - source cookbook is UNCHANGED (still owned by original owner)
    - new_owner can see new cookbook via list_cookbooks
    - Returns {status: 'forked', new_cookbook_id: …, parent_cookbook_id: …, …}

Authz:
  - Only cookbook owner (ctx.user_id == cookbook.bundle_owner) or master can handoff
  - new_owner must be a real User row
  - fails closed: unknown user → error, wrong owner → error
  - master key without user_id CAN handoff (admin path)
  - cbt_token callers cannot handoff (no user_id, not master)
"""
from __future__ import annotations

from typing import Generator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.models import Base, Bundle, BundleSkill, Skill, User
from app.mcp.tools.bundle_handoff import recipes_cookbook_handoff


# ─────────────────────────── Fixtures ───────────────────────────────────


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


# ─────────────────────────── Helpers ────────────────────────────────────


def _make_user(db: Session, tier: str = "pro") -> User:
    user = User(
        id=uuid4(),
        display_name="Test User",
        email=f"{uuid4()}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(user)
    db.flush()
    return user


def _make_cookbook(db: Session, owner_id: UUID, name: str = "My Cookbook") -> Bundle:
    cb = Bundle(
        id=uuid4(),
        name=name,
        is_base=False,
        bundle_owner=owner_id,
    )
    db.add(cb)
    db.flush()
    return cb


def _make_skill(db: Session, slug: str = "my-skill", public: bool = True) -> Skill:
    skill = Skill(
        id=uuid4(),
        slug=slug,
        title=f"Skill {slug}",
        description="x",
        is_public=public,
    )
    db.add(skill)
    db.flush()
    return skill


def _add_cookbook_skill(
    db: Session, cookbook_id: UUID, skill_id: UUID, source: str = "custom-added"
) -> BundleSkill:
    cs = BundleSkill(
        bundle_id=cookbook_id,
        skill_id=skill_id,
        source=source,
    )
    db.add(cs)
    db.flush()
    return cs


# ─────────────────────────── TRANSFER mode ──────────────────────────────


def test_transfer_changes_owner(db_session):
    """Transfer: cookbook_owner changes to new_owner; source cookbook updated in-place."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(user_b.id),
        mode="transfer",
    )

    assert result.get("status") == "transferred", result
    assert result["cookbook_id"] == str(cb.id)
    assert result["new_owner_user_id"] == str(user_b.id)

    db_session.refresh(cb)
    assert cb.bundle_owner == user_b.id


def test_transfer_new_owner_can_list(db_session):
    """After transfer, new owner sees the cookbook; old owner does not."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    recipes_cookbook_handoff(
        db_session, ctx=ctx, cookbook_id=str(cb.id),
        new_owner_user_id=str(user_b.id), mode="transfer",
    )

    # user_b owns it now
    b_books = db_session.query(Bundle).filter(Bundle.bundle_owner == user_b.id).all()
    assert any(book.id == cb.id for book in b_books)

    # user_a does not
    a_books = db_session.query(Bundle).filter(Bundle.bundle_owner == user_a.id).all()
    assert not any(book.id == cb.id for book in a_books)


def test_transfer_by_email(db_session):
    """Transfer by new_owner_email resolves to the correct user."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_email=user_b.email,
        mode="transfer",
    )

    assert result["status"] == "transferred"
    assert result["new_owner_user_id"] == str(user_b.id)

    db_session.refresh(cb)
    assert cb.bundle_owner == user_b.id


def test_transfer_master_can_handoff(db_session):
    """Master-key caller can transfer any cookbook."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext(scope="master")
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(user_b.id),
        mode="transfer",
    )
    assert result["status"] == "transferred"
    db_session.refresh(cb)
    assert cb.bundle_owner == user_b.id


# ─────────────────────────── FORK mode ──────────────────────────────────


def test_fork_creates_new_cookbook(db_session):
    """Fork: creates a NEW cookbook, source unchanged."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id, name="Source Book")
    skill = _make_skill(db_session)
    _add_cookbook_skill(db_session, cb.id, skill.id, source="custom-added")
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(user_b.id),
        mode="fork",
    )

    assert result["status"] == "forked", result
    assert "new_cookbook_id" in result
    new_cb_id = UUID(result["new_cookbook_id"])

    # Source unchanged
    db_session.refresh(cb)
    assert cb.bundle_owner == user_a.id

    # New cookbook created
    new_cb = db_session.query(Bundle).filter(Bundle.id == new_cb_id).first()
    assert new_cb is not None
    assert new_cb.bundle_owner == user_b.id


def test_fork_lineage_set(db_session):
    """Fork: new cookbook has parent_cookbook_id and synced_from_cookbook_id pointing to source."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(user_b.id),
        mode="fork",
    )

    new_cb = db_session.query(Bundle).filter(
        Bundle.id == UUID(result["new_cookbook_id"])
    ).first()
    assert new_cb.parent_bundle_id == cb.id
    assert new_cb.synced_from_bundle_id == cb.id
    assert result["parent_cookbook_id"] == str(cb.id)


def test_fork_copies_only_custom_added_skills(db_session):
    """Fork: only custom-added skills are copied; forked/disabled/overridden are not."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    skill_custom = _make_skill(db_session, slug="custom-skill")
    skill_forked = _make_skill(db_session, slug="forked-skill")
    skill_disabled = _make_skill(db_session, slug="disabled-skill")
    _add_cookbook_skill(db_session, cb.id, skill_custom.id, source="custom-added")
    _add_cookbook_skill(db_session, cb.id, skill_forked.id, source="forked")
    _add_cookbook_skill(db_session, cb.id, skill_disabled.id, source="disabled")
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(user_b.id),
        mode="fork",
    )

    new_cb_id = UUID(result["new_cookbook_id"])
    new_skills = db_session.query(BundleSkill).filter(
        BundleSkill.bundle_id == new_cb_id
    ).all()

    slugs_in_new = {
        db_session.query(Skill).filter(Skill.id == cs.skill_id).first().slug
        for cs in new_skills
    }
    assert "custom-skill" in slugs_in_new, slugs_in_new
    assert "forked-skill" not in slugs_in_new, slugs_in_new
    assert "disabled-skill" not in slugs_in_new, slugs_in_new


def test_fork_result_skill_count(db_session):
    """Fork: result includes copied_skills count."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    for i in range(3):
        skill = _make_skill(db_session, slug=f"skill-{i}")
        _add_cookbook_skill(db_session, cb.id, skill.id, source="custom-added")
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(user_b.id),
        mode="fork",
    )
    assert result["copied_skills"] == 3


def test_fork_new_owner_can_list(db_session):
    """Fork: new_owner sees the forked cookbook in their list."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id, name="To Fork")
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(user_b.id),
        mode="fork",
    )

    new_cb_id = UUID(result["new_cookbook_id"])
    b_books = db_session.query(Bundle).filter(Bundle.bundle_owner == user_b.id).all()
    assert any(book.id == new_cb_id for book in b_books)


# ─────────────────────────── AUTHZ failures ─────────────────────────────


def test_handoff_wrong_owner_rejected(db_session):
    """Non-owner cannot hand off a cookbook they don't own."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    user_c = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_c.id)  # user_c != owner
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(user_b.id),
        mode="transfer",
    )
    assert result.get("error") in ("forbidden", "cookbook_not_found"), result


def test_handoff_unknown_cookbook_rejected(db_session):
    """Handoff on non-existent cookbook → error."""
    user = _make_user(db_session)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(uuid4()),
        new_owner_user_id=str(uuid4()),
        mode="transfer",
    )
    assert result.get("error") in ("cookbook_not_found",), result


def test_handoff_unknown_new_owner_rejected(db_session):
    """Handoff to a non-existent user → error."""
    user_a = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(uuid4()),  # no such user
        mode="transfer",
    )
    assert result.get("error") == "new_owner_not_found", result


def test_handoff_no_new_owner_specified(db_session):
    """Handoff with neither new_owner_user_id nor new_owner_email → error."""
    user_a = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        mode="transfer",
    )
    assert result.get("error") == "new_owner_required", result


def test_handoff_invalid_mode(db_session):
    """Unknown mode → error."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(user_b.id),
        mode="clone",  # invalid
    )
    assert result.get("error") == "invalid_mode", result


def test_handoff_unauthenticated_rejected(db_session):
    """Anonymous / unauthenticated caller → error."""
    user_a = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext.anonymous()
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(cb.id),
        new_owner_user_id=str(user_a.id),
        mode="transfer",
    )
    assert result.get("error") in ("forbidden", "auth_required"), result


def test_handoff_base_cookbook_rejected(db_session):
    """Cannot hand off the base/system cookbook."""
    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    base_cb = Bundle(
        id=uuid4(),
        name="WiseChef Base",
        is_base=True,
        bundle_owner=None,  # base catalog has no owner
    )
    db_session.add(base_cb)
    db_session.commit()

    ctx = AuthContext(scope="master")
    result = recipes_cookbook_handoff(
        db_session,
        ctx=ctx,
        cookbook_id=str(base_cb.id),
        new_owner_user_id=str(user_b.id),
        mode="transfer",
    )
    assert result.get("error") == "cannot_handoff_base", result


# ─────────────────────────── MCP dispatch wiring ────────────────────────


def test_dispatch_wiring(db_session):
    """recipes_cookbook_handoff is reachable via call_tool_sync."""
    from app.mcp.server import call_tool_sync

    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id)
    db_session.commit()

    ctx = AuthContext(scope="user", user_id=user_a.id)
    result = call_tool_sync(
        "recipes_cookbook_handoff",
        {
            "cookbook_id": str(cb.id),
            "new_owner_user_id": str(user_b.id),
            "mode": "transfer",
        },
        caller={"scope": "user", "user_id": str(user_a.id), "auth_ctx": ctx},
        db=db_session,
    )
    assert result.get("status") == "transferred", result


# ─────────────────────────── REST endpoint wiring ───────────────────────


def test_rest_handoff_transfer(db_session):
    """POST /api/cookbooks/{id}/handoff with mode=transfer works via REST."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.database import get_db
    from app.bundle_routes import router as cookbook_router

    user_a = _make_user(db_session)
    user_b = _make_user(db_session)
    cb = _make_cookbook(db_session, owner_id=user_a.id, name="REST Test")
    db_session.commit()

    app = FastAPI()

    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db

    _uid = user_a.id

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = _uid
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(cookbook_router)

    client = TestClient(app, raise_server_exceptions=True)
    resp = client.post(
        f"/api/cookbooks/{cb.id}/handoff",
        json={"new_owner_user_id": str(user_b.id), "mode": "transfer"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "transferred"
    assert data["new_owner_user_id"] == str(user_b.id)
