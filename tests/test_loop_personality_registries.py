"""Tests for the loop + personality registry routes and MCP tools.

loopskill_0622 Phase 8 — the runnable catalog types pulled into v1.
Uses a self-contained FastAPI app wired to the in-memory SQLite db_session
fixture (the shared `client` fixture doesn't mount these new routers).
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth_ctx import AuthContext
from app.database import get_db
from app.loop_routes import router as loop_router
from app.mcp.tools.loopskill_catalog import (
    loopskill_get_loop,
    loopskill_get_personality,
    loopskill_search_loops,
    loopskill_search_personalities,
)
from app.personality_routes import router as personality_router


@pytest.fixture()
def app_client(db_session):
    """App with loop+personality routers and a stub auth middleware.

    The stub stamps an authenticated user AuthContext when the test sends
    x-test-auth: user, else anonymous — mirrors APIKeyMiddleware's contract
    without dragging in the full key-validation stack.
    """
    app = FastAPI()

    @app.middleware("http")
    async def _stub_auth(request: Request, call_next):
        if request.headers.get("x-test-auth") == "user":
            from uuid import uuid4

            request.state.auth_ctx = AuthContext(scope="user", user_id=uuid4())
        else:
            request.state.auth_ctx = AuthContext.anonymous()
        return await call_next(request)

    app.include_router(loop_router)
    app.include_router(personality_router)

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, raise_server_exceptions=True)


# ── valid payloads ──────────────────────────────────────────────────────────

VALID_LOOP = {
    "slug": "tdd-loop",
    "title": "TDD Loop",
    "description": "Run until the test suite is green.",
    "category": "development",
    "success_condition": "all tests pass",
    "verification_script": "pytest -q",
    "system_prompt": "You are a TDD loop. Make the failing tests pass.",
    "max_turns": 30,
    "budget_usd": 5.0,
    "tool_allowlist": ["terminal", "read_file", "patch"],
    "stopping_criteria": {
        "success": "pytest exits 0",
        "failure": "identical failure twice in a row",
        "budget": "5 USD spent",
    },
}

VALID_PERSONALITY = {
    "slug": "ruthless-mentor",
    "title": "Ruthless Mentor",
    "description": "Stress-tests your plan in attack mode.",
    "category": "strategy",
    "system_prompt": "You are a ruthless mentor. Find every flaw.",
    "config": {"temperature": 0.3, "model_pref": "opus"},
}


# ── loop routes ─────────────────────────────────────────────────────────────

def test_publish_loop_requires_auth(app_client):
    r = app_client.post("/api/loops", json=VALID_LOOP)
    assert r.status_code == 401


def test_publish_and_get_loop(app_client):
    r = app_client.post("/api/loops", json=VALID_LOOP, headers={"x-test-auth": "user"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["slug"] == "tdd-loop"
    assert body["max_turns"] == 30
    assert body["tool_allowlist"] == ["terminal", "read_file", "patch"]
    assert body["verification_script"] == "pytest -q"

    # detail
    d = app_client.get("/api/loops/tdd-loop")
    assert d.status_code == 200
    assert d.json()["success_condition"] == "all tests pass"
    assert set(d.json()["stopping_criteria"]) == {"success", "failure", "budget"}


def test_publish_loop_rejects_unsafe_contract(app_client):
    bad = dict(VALID_LOOP, slug="no-verify", verification_script="")
    r = app_client.post("/api/loops", json=bad, headers={"x-test-auth": "user"})
    assert r.status_code == 422
    assert "verification_script" in r.text


def test_publish_loop_rejects_unbounded_turns(app_client):
    bad = dict(VALID_LOOP, slug="runaway", max_turns=99999)
    r = app_client.post("/api/loops", json=bad, headers={"x-test-auth": "user"})
    assert r.status_code == 422
    assert "ceiling" in r.text


def test_duplicate_loop_slug_conflicts(app_client):
    h = {"x-test-auth": "user"}
    assert app_client.post("/api/loops", json=VALID_LOOP, headers=h).status_code == 201
    r2 = app_client.post("/api/loops", json=VALID_LOOP, headers=h)
    assert r2.status_code == 409


def test_list_loops_returns_published(app_client):
    app_client.post("/api/loops", json=VALID_LOOP, headers={"x-test-auth": "user"})
    r = app_client.get("/api/loops")
    assert r.status_code == 200
    slugs = [x["slug"] for x in r.json()]
    assert "tdd-loop" in slugs


def test_get_missing_loop_404(app_client):
    assert app_client.get("/api/loops/nope").status_code == 404


# ── personality routes ──────────────────────────────────────────────────────

def test_publish_personality_requires_auth(app_client):
    assert app_client.post("/api/personalities", json=VALID_PERSONALITY).status_code == 401


def test_publish_and_get_personality(app_client):
    r = app_client.post(
        "/api/personalities", json=VALID_PERSONALITY, headers={"x-test-auth": "user"}
    )
    assert r.status_code == 201, r.text
    assert r.json()["slug"] == "ruthless-mentor"

    d = app_client.get("/api/personalities/ruthless-mentor")
    assert d.status_code == 200
    assert d.json()["system_prompt"].startswith("You are a ruthless mentor")
    assert d.json()["config"]["model_pref"] == "opus"


def test_personality_missing_system_prompt_rejected(app_client):
    bad = dict(VALID_PERSONALITY, slug="empty", system_prompt="")
    r = app_client.post("/api/personalities", json=bad, headers={"x-test-auth": "user"})
    assert r.status_code == 422


def test_list_personalities(app_client):
    app_client.post(
        "/api/personalities", json=VALID_PERSONALITY, headers={"x-test-auth": "user"}
    )
    r = app_client.get("/api/personalities")
    assert "ruthless-mentor" in [x["slug"] for x in r.json()]


# ── MCP tools (direct, against db_session) ──────────────────────────────────

def test_mcp_search_and_get_loop(db_session):
    from app.models import Loop
    from uuid import uuid4

    db_session.add(
        Loop(
            id=uuid4(),
            slug="mcp-loop",
            title="MCP Loop",
            description="discoverable over mcp",
            success_condition="x",
            verification_script="true",
            system_prompt="y",
            max_turns=10,
            stopping_criteria={"success": "a", "failure": "b", "budget": "c"},
            tool_allowlist=["terminal"],
        )
    )
    db_session.flush()

    res = loopskill_search_loops(db_session, query="mcp")
    assert res["total"] == 1
    assert res["results"][0]["slug"] == "mcp-loop"
    assert res["results"][0]["max_turns"] == 10

    detail = loopskill_get_loop(db_session, slug="mcp-loop")
    assert detail["verification_script"] == "true"
    assert detail["tool_allowlist"] == ["terminal"]

    assert loopskill_get_loop(db_session, slug="ghost")["status"] == 404


def test_mcp_search_and_get_personality(db_session):
    from app.models import Personality
    from uuid import uuid4

    db_session.add(
        Personality(
            id=uuid4(),
            slug="mcp-persona",
            title="MCP Persona",
            description="discoverable",
            system_prompt="be helpful",
        )
    )
    db_session.flush()

    res = loopskill_search_personalities(db_session, query="mcp")
    assert res["total"] == 1
    detail = loopskill_get_personality(db_session, slug="mcp-persona")
    assert detail["system_prompt"] == "be helpful"
    assert loopskill_get_personality(db_session, slug="ghost")["status"] == 404
