"""tests/test_feedback_mcp.py — Stream 1 test suite.

9 test cases covering:
  1. recipes_feedback happy path
  2. recipes_feedback dedup (same signature within 7d)
  3. recipes_feedback per-tool window (11th call in 24h)
  4. recipes_feedback force=true override
  5. recipes_feedback cross-tool ceiling (31st total)
  6. recipes_feedback loop detector (3 in 5 min)
  7. recipes_request_recipe happy path
  8. recipes_report_skill_error happy path (RECIPES_REPORT_ERRORS=true)
  9. github_dispatch failure -> endpoint still returns ok=true (durable write)
"""
from __future__ import annotations

import os
import time as _time
import uuid
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.feedback_ratelimit as rl_module


FAKE_ISSUE_URL = "https://github.com/wisechef-ai/recipes-api/issues/42"


@pytest.fixture(autouse=True)
def reset_ratelimit():
    """Reset all in-process rate-limit buckets between tests."""
    rl_module.reset_all()
    yield
    rl_module.reset_all()


@pytest.fixture()
def feedback_client(db_session: Session):
    """TestClient that includes the feedback_v1 router + db override."""
    from app.database import get_db
    from app.feedback_v1_routes import router as feedback_v1_router
    from app.config import settings

    test_app = FastAPI()

    def override_get_db():
        yield db_session

    test_app.include_router(feedback_v1_router)
    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(
        test_app,
        headers={"x-api-key": settings.API_KEY},
        raise_server_exceptions=True,
    ) as c:
        yield c


# ── Test 1: recipes_feedback happy path ──────────────────────────────────────

def test_feedback_happy_path(feedback_client, db_session):
    """POST /api/v1/feedback returns 201 with ok=true; issue_url is pending ("").

    Post-topshelf_2605/B: dispatch_event returns True on success and the
    real issue URL is PATCHed back by the workflow via the internal route.
    issue_url is therefore empty at submit time — clients poll
    GET /api/feedback/{id} for the resolved URL.
    """
    with patch("app.feedback_v1_routes.github_dispatch.dispatch_event",
               return_value=True) as mock_dispatch:
        resp = feedback_client.post("/api/v1/feedback", json={
            "category": "ux",
            "message": "The search results are not relevant enough.",
        })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["issue_url"] == ""
    assert data["deduped"] is False
    assert data["id"] != ""
    mock_dispatch.assert_called_once()


# ── Test 2: recipes_feedback dedup ────────────────────────────────────────────

def test_feedback_dedup(feedback_client, db_session):
    """Same feedback signature within 7d returns deduped=True with cached issue_url."""
    payload = {
        "category": "billing",
        "message": "I was double-charged last month.",
    }
    with patch("app.feedback_v1_routes.github_dispatch.dispatch_event",
               return_value=True):
        resp1 = feedback_client.post("/api/v1/feedback", json=payload)
    assert resp1.status_code == 201
    first_url = resp1.json()["issue_url"]

    # Second identical submission
    with patch("app.feedback_v1_routes.github_dispatch.dispatch_event",
               return_value=True) as mock2:
        resp2 = feedback_client.post("/api/v1/feedback", json=payload)
    assert resp2.status_code == 201, resp2.text
    data2 = resp2.json()
    assert data2["ok"] is True
    assert data2["deduped"] is True
    assert data2["issue_url"] == first_url
    # dispatch should NOT be called again for dedup
    mock2.assert_not_called()


# ── Test 3: per-tool window (11th call → hard-block) ─────────────────────────

def test_feedback_per_tool_limit(db_session):
    """11th feedback in 24h triggers hard-block.

    Pre-fills the per-tool and cross-tool buckets directly to avoid
    triggering the loop detector (fires at 3 rapid submissions).
    """
    from app.database import get_db
    from app.feedback_v1_routes import router as feedback_v1_router
    from app.config import settings

    test_app = FastAPI()
    test_app.include_router(feedback_v1_router)
    test_app.dependency_overrides[get_db] = lambda: db_session

    identity = f"agent:pertool-test-{uuid.uuid4().hex}"
    now = _time.monotonic()

    # Pre-fill per-tool bucket with 10 entries
    with rl_module._lock:
        rl_module._per_tool[(identity, "feedback")] = [now - (i * 60) for i in range(10)]
        # Also populate cross-tool so ceiling doesn't fire first
        rl_module._cross_tool[identity] = [now - (i * 60) for i in range(10)]

    with TestClient(test_app, headers={"x-api-key": settings.API_KEY},
                    raise_server_exceptions=False) as c:
        with patch("app.feedback_v1_routes.github_dispatch.dispatch_event",
                   return_value=FAKE_ISSUE_URL):
            with patch("app.feedback_v1_routes._get_identity", return_value=identity):
                r = c.post("/api/v1/feedback", json={
                    "category": "other",
                    "message": f"Eleventh unique feedback {uuid.uuid4().hex}",
                })

    assert r.status_code == 429, r.text
    detail = r.json()["detail"]
    assert detail["force_available"] is True
    assert "last_submissions" in detail


# ── Test 4: force=true override ───────────────────────────────────────────────

def test_feedback_force_override(db_session):
    """force=True with confirmation bypasses the loop detector cooldown."""
    from app.database import get_db
    from app.feedback_v1_routes import router as feedback_v1_router
    from app.config import settings

    test_app = FastAPI()
    test_app.include_router(feedback_v1_router)
    test_app.dependency_overrides[get_db] = lambda: db_session

    identity = f"agent:force-test-{uuid.uuid4().hex}"

    with TestClient(test_app, headers={"x-api-key": settings.API_KEY},
                    raise_server_exceptions=False) as c:
        with patch("app.feedback_v1_routes.github_dispatch.dispatch_event",
                   return_value=FAKE_ISSUE_URL):
            with patch("app.feedback_v1_routes._get_identity", return_value=identity):
                # Trigger loop detector (3 rapid submissions)
                for i in range(3):
                    c.post("/api/v1/feedback", json={
                        "category": "ux",
                        "message": f"Loop test message {i} {uuid.uuid4().hex}",
                    })

                # Without force -> loop block
                r_blocked = c.post("/api/v1/feedback", json={
                    "category": "ux",
                    "message": f"Blocked message {uuid.uuid4().hex}",
                })

                # With force=True + confirmation -> should pass
                r_forced = c.post("/api/v1/feedback", json={
                    "category": "ux",
                    "message": f"Forced message {uuid.uuid4().hex}",
                    "force": True,
                    "confirmation": "yes I understand",
                })

    # Blocked call should be 429
    assert r_blocked.status_code == 429, r_blocked.text
    # Forced call should succeed
    assert r_forced.status_code == 201, r_forced.text
    assert r_forced.json()["ok"] is True


# ── Test 5: cross-tool ceiling (31st total → hard-block) ─────────────────────

def test_feedback_cross_tool_ceiling(db_session):
    """31st total submission across all tools from same identity triggers hard-block."""
    from app.database import get_db
    from app.feedback_v1_routes import router as feedback_v1_router
    from app.config import settings

    test_app = FastAPI()
    test_app.include_router(feedback_v1_router)
    test_app.dependency_overrides[get_db] = lambda: db_session

    identity = f"agent:cross-test-{uuid.uuid4().hex}"
    now = _time.monotonic()

    # Pre-fill cross-tool bucket to the ceiling (30 entries)
    with rl_module._lock:
        rl_module._cross_tool[identity] = [now - (i * 60) for i in range(30)]

    with TestClient(test_app, headers={"x-api-key": settings.API_KEY},
                    raise_server_exceptions=False) as c:
        with patch("app.feedback_v1_routes.github_dispatch.dispatch_event",
                   return_value=FAKE_ISSUE_URL):
            with patch("app.feedback_v1_routes._get_identity", return_value=identity):
                r = c.post("/api/v1/feedback", json={
                    "category": "docs",
                    "message": f"Cross-ceiling test {uuid.uuid4().hex}",
                })

    assert r.status_code == 429, r.text
    detail = r.json()["detail"]
    assert detail["force_available"] is False


# ── Test 6: loop detector (3 in 5 min → cooldown) ────────────────────────────

def test_feedback_loop_detector(db_session):
    """3 submissions in 5 min triggers 15-min cooldown."""
    from app.database import get_db
    from app.feedback_v1_routes import router as feedback_v1_router
    from app.config import settings

    test_app = FastAPI()
    test_app.include_router(feedback_v1_router)
    test_app.dependency_overrides[get_db] = lambda: db_session

    identity = f"agent:loop-{uuid.uuid4().hex}"

    with TestClient(test_app, headers={"x-api-key": settings.API_KEY},
                    raise_server_exceptions=False) as c:
        with patch("app.feedback_v1_routes.github_dispatch.dispatch_event",
                   return_value=FAKE_ISSUE_URL):
            with patch("app.feedback_v1_routes._get_identity", return_value=identity):
                # First 3 should succeed
                for i in range(3):
                    r = c.post("/api/v1/feedback", json={
                        "category": "search",
                        "message": f"Loop message {i} {uuid.uuid4().hex}",
                    })
                    assert r.status_code == 201, f"submission {i}: {r.text}"

                # 4th should be loop-blocked
                r4 = c.post("/api/v1/feedback", json={
                    "category": "search",
                    "message": f"Loop message 4 {uuid.uuid4().hex}",
                })

    assert r4.status_code == 429, r4.text
    detail = r4.json()["detail"]
    assert detail["error"] == "loop_detector_cooldown"
    assert "retry_at" in detail


# ── Test 7: recipes_request_recipe happy path ─────────────────────────────────

def test_recipify_request_happy_path(db_session):
    """POST /api/v1/recipify-request returns 201 with ok=true."""
    from app.database import get_db
    from app.feedback_v1_routes import router as feedback_v1_router
    from app.config import settings

    test_app = FastAPI()
    test_app.include_router(feedback_v1_router)
    test_app.dependency_overrides[get_db] = lambda: db_session

    with TestClient(test_app, headers={"x-api-key": settings.API_KEY},
                    raise_server_exceptions=True) as c:
        with patch("app.feedback_v1_routes.github_dispatch.dispatch_event",
                   return_value=True) as mock_dispatch:
            r = c.post("/api/v1/recipify-request", json={
                "target_name": "cognee-api-watchdog",
                "why_useful": "We need a recipe to monitor the Cognee API endpoints for drift.",
                "suggested_sources": ["https://docs.cognee.ai"],
                "agent_id": "test-agent-001",
            })

    assert r.status_code == 201, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["issue_url"] == ""
    assert data["deduped"] is False
    assert data["id"] != ""
    mock_dispatch.assert_called_once()
    call_args = mock_dispatch.call_args
    assert call_args[0][0] == "recipify-request"
    assert call_args[0][1]["target_name"] == "cognee-api-watchdog"


# ── Test 8: recipes_report_skill_error happy path ─────────────────────────────

@pytest.mark.skipif(
    os.environ.get("RECIPES_REPORT_ERRORS", "").lower() != "true",
    reason="RECIPES_REPORT_ERRORS not set to true",
)
def test_skill_error_happy_path_mcp(db_session):
    """recipes_report_skill_error MCP tool returns ok=true when RECIPES_REPORT_ERRORS=true."""
    from tests.conftest import make_skill
    from app.mcp.tools.skill_error import recipes_report_skill_error

    make_skill(db_session, slug="cognee-v1-api-migration")

    with patch("app.mcp.tools.skill_error.github_dispatch.dispatch_event",
               return_value=FAKE_ISSUE_URL) as mock_dispatch:
        result = recipes_report_skill_error(
            db_session,
            slug="cognee-v1-api-migration",
            signature="deadbeef1234abcd",
            summary="The skill fails on Python 3.12 due to deprecated asyncio.get_event_loop()",
            details="Traceback: ...",
            agent_id="test-agent-001",
        )

    assert result["ok"] is True, result
    assert result["accepted"] is True
    assert result["id"] != ""
    mock_dispatch.assert_called_once()
    call_args = mock_dispatch.call_args
    assert call_args[0][0] == "skill-error"


# ── Test 9: github_dispatch failure → durable write ──────────────────────────

def test_github_dispatch_failure_durable_write(db_session):
    """When github_dispatch returns None (failure), endpoint still returns ok=true."""
    from app.database import get_db
    from app.feedback_v1_routes import router as feedback_v1_router
    from app.config import settings

    test_app = FastAPI()
    test_app.include_router(feedback_v1_router)
    test_app.dependency_overrides[get_db] = lambda: db_session

    with TestClient(test_app, headers={"x-api-key": settings.API_KEY},
                    raise_server_exceptions=True) as c:
        with patch("app.feedback_v1_routes.github_dispatch.dispatch_event",
                   return_value=None) as mock_dispatch:
            r = c.post("/api/v1/feedback", json={
                "category": "install",
                "message": "Installation fails silently on Debian 12.",
            })

    assert r.status_code == 201, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["issue_url"] == ""   # empty string when dispatch failed
    assert data["id"] != ""          # DB row was still created
    mock_dispatch.assert_called_once()
