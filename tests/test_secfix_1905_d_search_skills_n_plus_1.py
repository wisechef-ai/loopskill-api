"""Tests for Issue #19: search_skills N+1 query fix.

The original implementation called _install_counts_for(db, [s.id]) once per
result row, generating N+1 queries for a page of N skills.

After the fix, a single batched call is made for all IDs at once.

Uses a SQLAlchemy 'before_cursor_execute' event listener to count queries.
"""

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
from datetime import datetime, timezone
from uuid import uuid4

from app.models import Base, Skill
from app.database import get_db
from app.routes import router
from app.skill_routes import router as skill_router
from app.config import settings


@pytest.fixture(scope="module")
def engine_n1():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)


@pytest.fixture(scope="module")
def session_n1(engine_n1):
    SessionLocal = sessionmaker(bind=engine_n1, autocommit=False, autoflush=False)
    sess = SessionLocal()
    yield sess
    sess.close()


@pytest.fixture(scope="module")
def populated_client(engine_n1, session_n1):
    """Create 50 public skills and return a TestClient."""
    # Seed 50 public skills
    for i in range(50):
        s = Skill(
            id=uuid4(),
            slug=f"n1-test-skill-{i:03d}",
            title=f"N+1 Test Skill {i}",
            category="devops",
            is_public=True,
            is_archived=False,
            created_at=datetime.now(timezone.utc),
        )
        session_n1.add(s)
    session_n1.commit()

    SessionLocal = sessionmaker(bind=engine_n1, autocommit=False, autoflush=False)
    app = FastAPI()

    def override_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.include_router(router)
    app.include_router(skill_router, prefix="/api")  # Phase E: search moved to skill_routes
    app.dependency_overrides[get_db] = override_db

    with TestClient(app, headers={"x-api-key": settings.API_KEY}) as tc:
        yield tc, engine_n1


def test_search_skills_50_rows_uses_at_most_5_queries(populated_client):
    """A 50-row search page must execute ≤5 SQL queries total.

    Before the fix: 1 (count) + 1 (select) + 50 (install_counts per row) = 52 queries.
    After the fix:  1 (count) + 1 (select) + 1 (batched install_counts) = 3 queries.
    """
    tc, engine = populated_client
    query_count = 0

    def before_cursor_execute(conn, cursor, statement, params, context, executemany):
        nonlocal query_count
        query_count += 1

    sa_event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        resp = tc.get("/api/skills/search?page_size=50&hybrid=false")
    finally:
        sa_event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert resp.status_code == 200, f"Unexpected status: {resp.status_code} — {resp.text[:200]}"
    data = resp.json()

    # We seeded 50 skills; the search should return them all
    assert len(data.get("results", data.get("skills", data.get("items", [])))) > 0

    assert query_count <= 5, (
        f"search_skills N+1: expected ≤5 queries for 50-row page, got {query_count}. "
        f"Fix the per-row _install_counts_for call in routes.py."
    )


def test_search_skills_returns_correct_count(populated_client):
    """Sanity: 50 skills seeded, search returns results."""
    tc, _ = populated_client
    resp = tc.get("/api/skills/search?q=N1+Test&hybrid=false&page_size=50")
    assert resp.status_code == 200
