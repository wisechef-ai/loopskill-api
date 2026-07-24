"""Self-contained test for transparency router."""

from __future__ import annotations

import os
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.transparency_routes import router as transparency_router
from app.database import get_db


@pytest.fixture()
def client(db_session):
    app = FastAPI()
    app.include_router(transparency_router)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_transparency_returns_200(client):
    r = client.get("/api/health/transparency")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "install_count_drift",
        "skill_error_rate_7d",
        "feedback_volume_7d",
        "median_issue_resolution_h",
        "computed_at",
        "ttl_seconds",
    ):
        assert key in body, key
    assert body["ttl_seconds"] == 60
    assert isinstance(body["install_count_drift"], int)
    assert 0.0 <= body["skill_error_rate_7d"] <= 1.0


def test_transparency_no_auth(client):
    r = client.get("/api/health/transparency")
    assert r.status_code == 200


def test_transparency_cached(client):
    r1 = client.get("/api/health/transparency").json()
    r2 = client.get("/api/health/transparency").json()
    assert r1["computed_at"] == r2["computed_at"]


def test_health_status_returns_200(client):
    """spotify_1507 Ph0: bare GET /api/health is public, always-200, no DB."""
    r = client.get("/api/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "loopskill-api"
    assert "version" in body


def test_health_status_no_db_dependency(client):
    """Liveness must not depend on the DB session — status page semantics."""
    app = FastAPI()
    app.include_router(transparency_router)
    # deliberately NO get_db override — the route must still 200
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.get("/api/health")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
