"""Shared test fixtures for WiseRecipes API tests.

Provides:
  engine_fixture — session-scoped SQLAlchemy engine. SQLite in-memory by
                   default; set TEST_DATABASE_URL to point the whole suite
                   at a real Postgres instead (mesh0408 T0-A CI parity —
                   production is Postgres, and SQLite silently accepts
                   things Postgres rejects: CHECK constraints, VARCHAR
                   length limits, strict boolean typing, etc.)
  db_session     — per-test transactional session using SAVEPOINT isolation
                   (F11: prevents commit() inside tests from leaking state)
  client         — FastAPI TestClient wired to the test DB

Engine selection (mesh0408 T0-A): the suite honours TEST_DATABASE_URL first,
then falls back to DATABASE_URL / WR_DATABASE_URL (the same vars app.config
reads) so a CI job can simply set DATABASE_URL to a postgres:// DSN and the
whole suite — app config AND this fixture — points at the same engine. If
none of those are set (local `pytest` with no env), SQLite in-memory is the
default, preserving today's zero-config behaviour.
"""

from __future__ import annotations

import os
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.models import Base, Skill


def _test_database_url() -> str:
    """Resolve which engine the test suite should run against.

    Priority: TEST_DATABASE_URL (test-specific override) > DATABASE_URL /
    WR_DATABASE_URL (same vars app.config.Settings reads, so CI can set one
    var and have both the app and the tests agree on the engine) > SQLite
    in-memory (unchanged default for local `pytest`).
    """
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or os.environ.get("WR_DATABASE_URL")
        or "sqlite:///:memory:"
    )


# ── Reusable helper (importable by other test modules) ─────────────────────


def make_skill(
    db,
    slug: str = "test-skill",
    title: str = "Test Skill",
    category: str = "devops",
    is_public: bool = True,
    **kwargs,
) -> "Skill":
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
def engine_fixture(worker_id):
    """Engine shared for the entire test session.

    mesh0408 T0-A: engine-aware. Defaults to in-memory SQLite (unchanged
    behaviour); honours TEST_DATABASE_URL / DATABASE_URL / WR_DATABASE_URL
    to run the identical suite against Postgres so CHECK constraints and
    other dialect-specific behaviour actually get exercised (production is
    Postgres — SQLite alone never caught these by construction).

    mesh0408 T0-A Postgres run caught a real xdist race: pytest-xdist runs
    multiple worker PROCESSES in parallel (``-n auto --dist loadfile``),
    each with its own session-scoped engine_fixture. Against SQLite that
    was silently safe because ``sqlite:///:memory:`` gives each process its
    own private database. Against a real, shared Postgres server every
    worker's ``Base.metadata.create_all`` / ``drop_all`` hit the SAME
    database concurrently — one worker dropping a table while another was
    mid-transaction against it, surfaced as sqlalchemy.exc.OperationalError
    ("relation does not exist") across ~170 unrelated tests. Fix: give each
    xdist worker its own Postgres schema (``pytest_gw0``, ``pytest_gw1``,
    ...) via ``search_path``, so DDL and data are isolated per worker the
    same way SQLite's per-process file already isolated them. The
    non-distributed run (``worker_id == "master"``, e.g. local `pytest`
    with no `-n`) keeps using the default `public` schema unchanged.
    """
    db_url = _test_database_url()

    if db_url.startswith("sqlite"):
        engine = create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(conn, _record):
            conn.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(bind=engine)
        yield engine
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        return

    # Postgres (or any other real server-based dialect).
    schema = None
    connect_args: dict = {}
    if worker_id != "master":
        # Running under pytest-xdist: isolate this worker's DDL/data into
        # its own schema so parallel workers never race on the same tables.
        schema = f"pytest_{worker_id}"
        connect_args["options"] = f"-csearch_path={schema}"

    engine = create_engine(db_url, pool_pre_ping=True, connect_args=connect_args)

    if schema:
        with engine.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            conn.commit()

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    if schema:
        with engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            conn.commit()
    engine.dispose()


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
    transaction = connection.begin()  # outer transaction (always rolls back)
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

    Uses a minimal FastAPI app that includes only the main routes router,
    skipping creator_routes / publisher_routes which drag in stripe/jwt
    dependencies that aren't always installed in the test env.
    """
    from app.config import settings
    from app.database import get_db

    from fastapi import FastAPI

    test_app = FastAPI()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    # bootcamp_0607: curated install curricula
    try:
        from app.bootcamp_routes import router as bootcamp_router

        test_app.include_router(bootcamp_router, prefix="/api")
    except Exception:
        pass

    # Also include core routes (skills, telemetry) if importable
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
        from app.metasearch_routes import router as metasearch_router
        from app.metasearch_deploy_routes import router as metasearch_deploy_router

        test_app.include_router(
            metasearch_router
        )  # metasearch_0710 P0 — BEFORE skill_router so /metasearch beats /{slug}
        test_app.include_router(metasearch_deploy_router)  # metasearch_0710 P3 fleet-deploy
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

    # REVENUE/CATALOG (atomic-habits fallback 2026-07-18): curated loop packs
    try:
        from app.loop_pack_routes import router as loop_pack_router

        test_app.include_router(loop_pack_router)
    except Exception:
        pass

    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(
        test_app,
        headers={"x-api-key": settings.API_KEY},
        raise_server_exceptions=True,
    ) as c:
        yield c
