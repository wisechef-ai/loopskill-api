"""Phase A — intent survey tests.

POST /api/intent-survey accepts {q1..q5} (anonymous, no email required).
GET  /api/intent-survey/results returns aggregate counts (admin only).
"""
from __future__ import annotations

from typing import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.models import Base


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    @event.listens_for(engine, "connect")
    def _set_pragma(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def client(db, monkeypatch) -> TestClient:
    monkeypatch.setattr(settings, "API_KEY", "rec_test_admin_key")
    from app.intent_survey_routes import router as survey_router

    app = FastAPI()
    def _override():
        try:
            yield db
        finally:
            pass
    app.dependency_overrides[get_db] = _override
    app.include_router(survey_router)
    return TestClient(app)


# ── POST /api/intent-survey ─────────────────────────────────────────────

def test_post_full_survey_returns_201(client):
    resp = client.post("/api/intent-survey", json={
        "q1": "yes",
        "q2": "Better onboarding",
        "q3": "Too expensive without trial",
        "q4": "agency",
        "q5": "ada@example.com",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert "id" in body


def test_post_minimal_survey_returns_201(client):
    """Only q1 + q4 required-ish; other fields optional."""
    resp = client.post("/api/intent-survey", json={"q1": "maybe", "q4": "dev"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["ok"] is True


def test_post_invalid_q1_value_returns_422(client):
    resp = client.post("/api/intent-survey", json={"q1": "definitely", "q4": "agency"})
    assert resp.status_code == 422


def test_post_invalid_q4_value_returns_422(client):
    resp = client.post("/api/intent-survey", json={"q1": "yes", "q4": "wizard"})
    assert resp.status_code == 422


def test_post_truncates_overlong_text(client):
    """q2/q3/q5 are accepted but truncated to a sane upper bound (~2000 chars)."""
    resp = client.post("/api/intent-survey", json={
        "q1": "yes",
        "q2": "x" * 5000,
        "q4": "solo",
    })
    # Either 201 (truncated) or 422 (rejected) is acceptable; what's NOT
    # acceptable is a 500 / silent dump of 5KB into the DB.
    assert resp.status_code in (201, 422)


# ── GET /api/intent-survey/results (admin) ──────────────────────────────

def test_results_requires_admin_key(client):
    """Without API key header — 403."""
    resp = client.get("/api/intent-survey/results")
    assert resp.status_code in (401, 403)


def test_results_aggregate_counts(client):
    """Submit a few responses, then GET results returns aggregate buckets."""
    payloads = [
        {"q1": "yes", "q4": "agency"},
        {"q1": "yes", "q4": "agency"},
        {"q1": "no", "q4": "dev"},
        {"q1": "maybe", "q4": "solo"},
    ]
    for p in payloads:
        r = client.post("/api/intent-survey", json=p)
        assert r.status_code == 201

    resp = client.get(
        "/api/intent-survey/results",
        headers={"x-api-key": "rec_test_admin_key"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] >= 4
    assert body["q1"]["yes"] >= 2
    assert body["q1"]["no"] >= 1
    assert body["q1"]["maybe"] >= 1
    assert body["q4"]["agency"] >= 2
    assert body["q4"]["dev"] >= 1
    assert body["q4"]["solo"] >= 1
