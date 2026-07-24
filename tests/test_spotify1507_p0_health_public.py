"""spotify_1507 Phase 0 — bare GET /api/health must be public at the real
APIKeyMiddleware seam.

Regression: before Ph0, only /api/health/transparency was exempt; the bare
/api/health 401'd ("Invalid or missing x-api-key header") — a trust leak on
the cold-agent path (a status URL should never demand a key). This mirrors the
loopclose_3005 /skill public-middleware regression pattern: assert the path is
public through the production-wired app AND grep-guard EXEMPT_PATHS so a future
refactor can't silently re-break it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    connection = engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        Base.metadata.drop_all(bind=engine)


def test_api_health_is_public_no_key_needed(_db, monkeypatch):
    """/api/health must 200 through the real middleware with NO x-api-key."""
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=_db, monkeypatch=monkeypatch)
    client = TestClient(app)
    r = client.get("/api/health", follow_redirects=False)  # deliberately no key
    assert r.status_code == 200, (
        f"/api/health must be public at the middleware seam, got {r.status_code}: {r.text[:200]}"
    )
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "loopskill-api"


def test_api_health_in_exempt_paths_grep_guard():
    """Grep guard — dropping /api/health from EXEMPT_PATHS re-breaks the Ph0 fix."""
    from app.middleware.api_key import APIKeyMiddleware

    assert "/api/health" in APIKeyMiddleware.EXEMPT_PATHS, (
        "APIKeyMiddleware no longer exempts /api/health — re-introduces the "
        "spotify_1507 Ph0 bare-401 trust leak. Restore it in EXEMPT_PATHS."
    )
