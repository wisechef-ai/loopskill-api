"""An archived or non-public skill must not be reachable through the write rails.

Background: the `humanizer` skill was archived and made non-public, yet the
[skill-error] rail (REST ``POST /api/v1/skill-error`` and the MCP tool
``loopskill_report_skill_error``) kept resolving it by bare slug and kept filing
public GitHub issues for it; ``GET /api/skills/install`` resolved it too. The
lookup route (``GET /api/skills/{slug}``) already answers 404 for such a skill
(``Skill.is_public == True, Skill.is_archived == False``). These tests pin that
the other three entry points answer with the same 404 and never reach
``github_dispatch``.

Contract pinned here:
  * skill-error (REST + MCP): archived OR non-public -> 404 / "skill not found",
    no IncidentReport row, no dispatch. No owner/admin exception: the rail
    files issues on a public repository, so a private slug must never be named.
  * install: archived -> 404 for EVERY caller (admin and creator included).
    Non-public stays installable only by the admin key or the creator
    (documented self-install contract; pinned by the private-skill tests in
    test_polish_1805_free_install_no_auth.py), everyone else gets the same 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import make_skill

_SIG = "deadbeef12345678"


class _AllowedRL:
    allowed = True
    deduped = False
    issue_url = None
    force_available = False


@pytest.fixture(autouse=True)
def _no_real_ratelimit_or_optout(monkeypatch):
    """The guard under test sits BEFORE the rate limiters; stub them so the
    LIVE-slug control tests are deterministic and order-independent."""
    monkeypatch.delenv("RECIPES_REPORT_ERRORS", raising=False)
    from app import feedback_ratelimit

    monkeypatch.setattr(feedback_ratelimit, "check_and_record", lambda **kw: _AllowedRL())
    monkeypatch.setattr(feedback_ratelimit, "check_skill_error_backstop", lambda identity: True)
    from app import skill_error_routes

    monkeypatch.setattr(skill_error_routes, "_check_rate_limit", lambda fp: True)


def _seed_with_version(
    db, *, slug: str, is_public: bool = True, is_archived: bool = False, tier: str = "free"
):
    from app.models import SkillVersion

    sk = make_skill(db, slug=slug, is_public=is_public, tier=tier, is_archived=is_archived)
    db.add(
        SkillVersion(
            id=uuid.uuid4(),
            skill_id=sk.id,
            semver="1.0.0",
            tarball_size_bytes=1024,
            checksum_sha256="deadbeef" * 8,
            created_at=datetime.now(UTC),
        )
    )
    db.flush()
    return sk


@pytest.fixture
def client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    return TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))


@pytest.fixture
def master_headers(monkeypatch):
    """The REST rail sits behind APIKeyMiddleware; authenticate as the master key."""
    from app.config import settings

    key = getattr(settings, "API_KEY", None) or "guard-test-master-key"
    monkeypatch.setattr(settings, "API_KEY", key, raising=False)
    return {"x-api-key": key}


def _rest_report(client, slug, headers):
    return client.post(
        "/api/v1/skill-error",
        headers=headers,
        json={
            "skill_slug": slug,
            "error_signature": _SIG,
            "agent_fp_anon": "agentfp-guard-test-0001",
            "stack_trace_top": "boom",
        },
    )


def _incident_count(db) -> int:
    from app.models import IncidentReport

    return db.query(IncidentReport).count()


# ── REST skill-error ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"is_archived": True, "is_public": True},
        {"is_archived": False, "is_public": False},
        {"is_archived": True, "is_public": False},
    ],
    ids=["archived", "non-public", "archived+non-public"],
)
def test_rest_skill_error_refuses_archived_or_nonpublic(client, db_session, master_headers, kwargs):
    _seed_with_version(db_session, slug="gone-skill", **kwargs)
    before = _incident_count(db_session)
    with patch("app.skill_error_routes.github_dispatch.dispatch_event", return_value=True) as dispatch:
        resp = _rest_report(client, "gone-skill", master_headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "skill not found: gone-skill"
    dispatch.assert_not_called()
    assert _incident_count(db_session) == before, "no IncidentReport may be written for a refused slug"


def test_rest_skill_error_still_accepts_live_public_skill(client, db_session, master_headers):
    """Control: the guard must not break the happy path."""
    _seed_with_version(db_session, slug="live-skill")
    with patch("app.skill_error_routes.github_dispatch.dispatch_event", return_value=True) as dispatch:
        resp = _rest_report(client, "live-skill", master_headers)
    assert resp.status_code == 201, resp.text
    dispatch.assert_called_once()


# ── MCP skill-error ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"is_archived": True, "is_public": True},
        {"is_archived": False, "is_public": False},
    ],
    ids=["archived", "non-public"],
)
def test_mcp_skill_error_refuses_archived_or_nonpublic(db_session, kwargs):
    from app.mcp.tools.skill_error import loopskill_report_skill_error

    _seed_with_version(db_session, slug="gone-mcp-skill", **kwargs)
    before = _incident_count(db_session)
    with patch("app.mcp.tools.skill_error.github_dispatch.dispatch_event", return_value=True) as dispatch:
        result = loopskill_report_skill_error(
            db_session, slug="gone-mcp-skill", signature=_SIG, summary="boom", agent_id="guard-test"
        )
    assert result["ok"] is False, result
    assert result["error"] == "skill not found: gone-mcp-skill"
    dispatch.assert_not_called()
    assert _incident_count(db_session) == before


def test_mcp_skill_error_still_accepts_live_public_skill(db_session):
    from app.mcp.tools.skill_error import loopskill_report_skill_error

    _seed_with_version(db_session, slug="live-mcp-skill")
    with patch("app.mcp.tools.skill_error.github_dispatch.dispatch_event", return_value=True) as dispatch:
        result = loopskill_report_skill_error(
            db_session, slug="live-mcp-skill", signature=_SIG, summary="boom", agent_id="guard-test"
        )
    assert result["ok"] is True, result
    dispatch.assert_called_once()


# ── install ─────────────────────────────────────────────────────────────


def test_install_refuses_archived_skill_anonymous(client, db_session):
    _seed_with_version(db_session, slug="archived-install", is_archived=True)
    resp = client.get("/api/skills/install?slug=archived-install")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Skill 'archived-install' not found"


def test_install_refuses_archived_skill_even_for_the_admin_key(client, db_session, master_headers):
    """No caller class may install an archived skill — admin included."""
    _seed_with_version(db_session, slug="archived-admin-install", is_archived=True)
    resp = client.get("/api/skills/install?slug=archived-admin-install", headers=master_headers)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Skill 'archived-admin-install' not found"


def test_install_refuses_nonpublic_skill_anonymous(client, db_session):
    _seed_with_version(db_session, slug="private-install", is_public=False)
    resp = client.get("/api/skills/install?slug=private-install")
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Skill 'private-install' not found"


def test_install_still_serves_live_public_free_skill(client, db_session):
    """Control: a live public free skill is not caught by the guard."""
    _seed_with_version(db_session, slug="live-install")
    resp = client.get("/api/skills/install?slug=live-install")
    assert resp.status_code != 404, resp.text
    assert resp.status_code != 401, resp.text
