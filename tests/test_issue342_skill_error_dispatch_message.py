"""RED-proof regression tests for issue #342.

Root cause: neither the MCP tool (loopskill_report_skill_error) nor the REST
endpoint (POST /api/v1/skill-error) ever put a `message`/`category` key into
the github_dispatch.dispatch_event("skill-error", payload) call. The
receiving workflow (.github/workflows/feedback-dispatch.yml) falls back to
`payload.message || 'No message provided'` — so every skill-error report,
regardless of how detailed the caller's summary/details/stack_trace_top was,
filed as a content-free "[general] No message provided" issue.

These tests pin: (1) the payload always carries message+category, (2) the
message is bounded even after the [slug] prefix is applied, (3) a non-empty
message is produced from whatever partial input is available.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.mcp.tools import skill_error as mcp_skill_error
from app.skill_error_routes import _dispatch_message as rest_dispatch_message


# ── MCP path ──────────────────────────────────────────────────────────────


class _FakeRateLimitResult:
    allowed = True
    deduped = False
    issue_url = None
    force_available = False


def _make_db_with_skill(skill_id="skill-uuid-1"):
    db = MagicMock()
    skill = MagicMock()
    skill.id = skill_id
    db.query.return_value.filter.return_value.first.return_value = skill
    return db


@pytest.fixture(autouse=True)
def _stub_ratelimit_and_dispatch(monkeypatch):
    monkeypatch.setattr(
        mcp_skill_error.feedback_ratelimit,
        "check_and_record",
        lambda **kw: _FakeRateLimitResult(),
    )
    monkeypatch.setattr(
        mcp_skill_error.feedback_ratelimit,
        "check_skill_error_backstop",
        lambda identity: True,
    )
    yield


def test_mcp_skill_error_dispatch_carries_message_and_category():
    """RED on pre-fix code: KeyError on payload['message'] — issue #342."""
    db = _make_db_with_skill()
    captured = {}

    def fake_dispatch(event_type, payload):
        captured["payload"] = payload
        return True

    with patch.object(mcp_skill_error.github_dispatch, "dispatch_event", side_effect=fake_dispatch):
        result = mcp_skill_error.loopskill_report_skill_error(
            db,
            slug="my-skill",
            signature="deadbeef",
            summary="the install step fails on a fresh macOS host",
        )

    assert result["ok"] is True
    payload = captured["payload"]
    # This is the line that KeyErrors / fails pre-fix.
    assert payload["message"], "dispatch payload must carry a non-empty message"
    assert payload["category"] == "skill-error"
    assert "my-skill" in payload["message"]
    assert "install step fails" in payload["message"]


def test_mcp_skill_error_dispatch_message_bounded_when_summary_huge():
    """5000-char summary must not blow past the bound AFTER [slug] prefixing."""
    db = _make_db_with_skill()
    captured = {}

    def fake_dispatch(event_type, payload):
        captured["payload"] = payload
        return True

    huge_summary = "x" * 5000
    with patch.object(mcp_skill_error.github_dispatch, "dispatch_event", side_effect=fake_dispatch):
        mcp_skill_error.loopskill_report_skill_error(
            db,
            slug="a-very-long-skill-slug-name-indeed",
            signature="deadbeef",
            summary=huge_summary,
        )

    message = captured["payload"]["message"]
    assert len(message) <= mcp_skill_error._DISPATCH_MESSAGE_MAX
    assert message.startswith("[a-very-long-skill-slug-name-indeed]")


def test_mcp_skill_error_dispatch_message_present_when_summary_empty_details_only():
    """Empty summary + only details supplied must still yield a non-empty message."""
    db = _make_db_with_skill()
    captured = {}

    def fake_dispatch(event_type, payload):
        captured["payload"] = payload
        return True

    with patch.object(mcp_skill_error.github_dispatch, "dispatch_event", side_effect=fake_dispatch):
        mcp_skill_error.loopskill_report_skill_error(
            db,
            slug="my-skill",
            signature="deadbeef",
            summary="",
            details="only details provided here, no summary text at all",
        )

    message = captured["payload"]["message"]
    assert message
    assert "only details provided" in message


def test_mcp_skill_error_dispatch_message_generic_fallback_when_both_empty():
    """No summary, no details -> a generic non-empty placeholder, never blank."""
    db = _make_db_with_skill()
    captured = {}

    def fake_dispatch(event_type, payload):
        captured["payload"] = payload
        return True

    with patch.object(mcp_skill_error.github_dispatch, "dispatch_event", side_effect=fake_dispatch):
        mcp_skill_error.loopskill_report_skill_error(
            db,
            slug="my-skill",
            signature="deadbeef",
            summary="",
        )

    message = captured["payload"]["message"]
    assert message
    assert "my-skill" in message


# ── REST path ─────────────────────────────────────────────────────────────


def test_rest_skill_error_dispatch_carries_message_and_category():
    """RED on pre-fix code: AssertionError — payload missing 'message', issue #342."""
    message = rest_dispatch_message("my-skill", "TypeError: boom at line 42", "run.sh --deploy")
    assert message
    assert "my-skill" in message
    assert "TypeError" in message


def test_rest_dispatch_message_bounded_when_stack_trace_huge():
    huge_trace = "y" * 5000
    message = rest_dispatch_message("skill-slug", huge_trace, None)
    assert len(message) <= 500
    assert message.startswith("[skill-slug]")


def test_rest_dispatch_message_falls_back_to_command_when_stack_trace_empty():
    message = rest_dispatch_message("skill-slug", "", "python run.py")
    assert message
    assert "python run.py" in message


def test_rest_dispatch_message_generic_fallback_when_both_empty():
    message = rest_dispatch_message("skill-slug", None, None)
    assert message
    assert "skill-slug" in message
