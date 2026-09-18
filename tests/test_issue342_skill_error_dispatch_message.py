"""Regression test for issue #342.

Both `loopskill_report_skill_error` (MCP tool) and `POST /api/v1/skill-error`
(REST) built their `github_dispatch.dispatch_event("skill-error", payload)`
call WITHOUT a `message` or `category` key. The Feedback Dispatcher workflow
(.github/workflows/feedback-dispatch.yml) falls back to
`payload.message || 'No message provided'` and `payload.category || 'general'`
when those keys are absent — which is exactly the noise issue #342 filed:
"[general] No message provided".

This test pins that the dispatched payload always carries a real,
human-readable `message` (derived from the caller-supplied summary/details on
the MCP path, or from the anonymized stack_trace_top/command on the REST
path) and a `category` of "skill-error", so the workflow never falls back to
the placeholder title again.

RED-proof: run against pre-fix `app/mcp/tools/skill_error.py` /
`app/skill_error_routes.py` (message/category keys absent from the dispatch
call) — both assertions below fail with a KeyError, matching issue #342's
symptom byte-for-byte.
"""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.models import Skill


def _make_skill(db, slug="skill-error-issue342"):
    s = Skill(id=uuid4(), slug=slug, title="issue 342 target", is_public=True)
    db.add(s)
    db.flush()
    return s


# ── MCP tool: loopskill_report_skill_error ───────────────────────────────


def test_mcp_skill_error_dispatch_carries_message_and_category(db_session, monkeypatch):
    monkeypatch.setenv("RECIPES_REPORT_ERRORS", "true")
    from app.mcp.tools.skill_error import loopskill_report_skill_error

    _make_skill(db_session, slug="humanizer")
    db_session.commit()

    with patch(
        "app.mcp.tools.skill_error.github_dispatch.dispatch_event", return_value=True
    ) as mock_dispatch:
        result = loopskill_report_skill_error(
            db_session,
            slug="humanizer",
            signature="deadbeef1234abcd",
            summary="Skill ships broken curly-quote instructions",
            details="pattern 18's before/after examples are identical",
            agent_id="test-agent-342",
        )

    assert result["ok"] is True, result
    mock_dispatch.assert_called_once()
    call_args, _ = mock_dispatch.call_args
    payload = call_args[1]
    assert payload.get("message"), "dispatch payload missing 'message' — issue #342 regression"
    assert "curly-quote" in payload["message"]
    assert payload.get("category") == "skill-error", "dispatch payload missing 'category': 'skill-error'"


def test_mcp_skill_error_dispatch_message_bounded_when_summary_huge(db_session, monkeypatch):
    """Boundary: a huge summary must not blow up the GitHub issue title (256 char cap)."""
    monkeypatch.setenv("RECIPES_REPORT_ERRORS", "true")
    from app.mcp.tools.skill_error import loopskill_report_skill_error

    _make_skill(db_session, slug="huge-summary-skill")
    db_session.commit()

    huge_summary = "x" * 5000

    with patch(
        "app.mcp.tools.skill_error.github_dispatch.dispatch_event", return_value=True
    ) as mock_dispatch:
        result = loopskill_report_skill_error(
            db_session,
            slug="huge-summary-skill",
            signature="cafebabe12345678",
            summary=huge_summary,
            agent_id="test-agent-342b",
        )

    assert result["ok"] is True, result
    call_args, _ = mock_dispatch.call_args
    payload = call_args[1]
    assert len(payload["message"]) <= 500


def test_mcp_skill_error_dispatch_message_present_when_summary_empty_details_only(db_session, monkeypatch):
    """Empty-input edge: summary is required by the tool signature (str, not optional),
    but an empty string must still produce a non-crashing, non-empty dispatch message
    rather than silently degrading to the workflow's 'No message provided' fallback."""
    monkeypatch.setenv("RECIPES_REPORT_ERRORS", "true")
    from app.mcp.tools.skill_error import loopskill_report_skill_error

    _make_skill(db_session, slug="empty-summary-skill")
    db_session.commit()

    with patch(
        "app.mcp.tools.skill_error.github_dispatch.dispatch_event", return_value=True
    ) as mock_dispatch:
        result = loopskill_report_skill_error(
            db_session,
            slug="empty-summary-skill",
            signature="0123456789abcdef",
            summary="",
            details="only details provided, summary was blank",
            agent_id="test-agent-342c",
        )

    assert result["ok"] is True, result
    call_args, _ = mock_dispatch.call_args
    payload = call_args[1]
    assert payload["message"], "empty summary must not produce an empty dispatch message"
    assert "only details provided" in payload["message"]


# ── REST endpoint: POST /api/v1/skill-error ──────────────────────────────


@pytest.fixture
def skill_error_client(db_session):
    from app.skill_error_routes import router as skill_error_router

    app = FastAPI()
    app.include_router(skill_error_router)

    def override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_rest_skill_error_dispatch_carries_message_and_category(skill_error_client, db_session, monkeypatch):
    monkeypatch.setenv("RECIPES_REPORT_ERRORS", "true")
    skill = _make_skill(db_session, slug="rest-skill-error-target")
    db_session.commit()

    with patch("app.skill_error_routes.github_dispatch.dispatch_event", return_value=True) as mock_dispatch:
        resp = skill_error_client.post(
            "/api/v1/skill-error",
            json={
                "skill_slug": skill.slug,
                "error_signature": "abc123def4567890" + "0" * 16,
                "env_fingerprint": {"os": "linux"},
                "agent_fp_anon": "agent-fp-issue342-rest",
                "command": "run --broken-flag",
                "exit_code": 1,
                "stack_trace_top": "humanizer.py:18: identical before/after examples",
            },
        )

    assert resp.status_code == 201, resp.text
    mock_dispatch.assert_called_once()
    call_args, _ = mock_dispatch.call_args
    payload = call_args[1]
    assert payload.get("message"), "REST dispatch payload missing 'message' — issue #342 regression"
    assert "humanizer.py:18" in payload["message"]
    assert payload.get("category") == "skill-error"
