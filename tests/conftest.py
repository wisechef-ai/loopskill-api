"""Shared test fixtures for WiseRecipes API tests.

Provides:
  engine_fixture — in-memory SQLite engine (session-scoped)
  db_session     — per-test transactional session using SAVEPOINT isolation
                   (F11: prevents commit() inside tests from leaking state)
  client         — FastAPI TestClient wired to the in-memory DB
"""
from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Skill


# ── Reusable helper (importable by other test modules) ─────────────────────

def make_skill(db, slug: str = "test-skill", title: str = "Test Skill",
               category: str = "devops", is_public: bool = True, **kwargs) -> "Skill":
    """Create and flush a Skill row.  Returns the Skill instance."""
    from uuid import uuid4
    from datetime import datetime, timezone
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=title,
        category=category,
        is_public=is_public,
        created_at=datetime.now(timezone.utc),
        **kwargs,
    )
    db.add(s)
    db.flush()
    return s


@pytest.fixture(scope="session")
def engine_fixture():
    """In-memory SQLite engine shared for the entire test session."""
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
def db_session(engine_fixture) -> Generator[Session, None, None]:
    """Per-test transactional session using SAVEPOINT isolation.

    F11 fix: uses begin_nested() (SAVEPOINT) so that session.commit() inside
    tests only releases the inner SAVEPOINT, not the outer transaction. The
    outer transaction is always rolled back after each test, guaranteeing
    full isolation regardless of whether the test code calls commit().

    Reference: SQLAlchemy docs — "Joining a Session into an External Transaction"
    """
    connection = engine_fixture.connect()
    transaction = connection.begin()          # outer transaction (always rolls back)
    _SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = _SessionLocal()

    # Start a SAVEPOINT inside the outer transaction
    nested = connection.begin_nested()

    # Re-issue a SAVEPOINT each time the session commits, so the outer
    # transaction boundary stays intact.
    @event.listens_for(session, "after_transaction_end")
    def restart_savepoint(session, transaction):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def client(db_session: Session):
    """TestClient wired to the in-memory SQLite session.

    Uses a minimal FastAPI app that includes only the carousel router and the
    main routes router, skipping creator_routes / publisher_routes which drag
    in stripe/jwt dependencies that aren't always installed in the test env.
    """
    from app.config import settings
    from app.database import get_db
    from app.carousel.routes import router as carousel_router

    from fastapi import FastAPI

    test_app = FastAPI()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    test_app.include_router(carousel_router, prefix="/api")

    # bootcamp_0607: curated install curricula
    try:
        from app.bootcamp_routes import router as bootcamp_router
        test_app.include_router(bootcamp_router, prefix="/api")
    except Exception:
        pass

    # Also include core routes (skills, telemetry, carousel legacy) if importable
    try:
        from app.routes import router as core_router
        test_app.include_router(core_router)
    except Exception:
        pass

    # Phase E: include the new feature routers split from routes.py
    try:
        from app.skill_routes import router as skill_router
        from app.install_routes import router as install_router
        from app.access_routes import router as access_router
        from app.recipe_routes import router as recipe_router
        from app.health_routes import router as health_router
        test_app.include_router(skill_router, prefix="/api")
        test_app.include_router(install_router, prefix="/api")
        test_app.include_router(access_router, prefix="/api")
        test_app.include_router(recipe_router, prefix="/api")
        test_app.include_router(health_router, prefix="/api")
    except Exception:
        pass

    # Include checkout + creator routes for Stripe/webhook tests
    try:
        from app.checkout_routes import router as checkout_router
        from app.creator_routes import router as creator_router
        test_app.include_router(checkout_router)
        test_app.include_router(creator_router)
    except Exception:
        pass

    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(
        test_app,
        headers={"x-api-key": settings.API_KEY},
        raise_server_exceptions=True,
    ) as c:
        yield c
