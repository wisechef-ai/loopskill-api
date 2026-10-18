"""qa1018 W3 — C20 (federation propose input validation) + C24 (OAuth next=).

C20 / BRK-1: POST /api/federation/propose accepted ``javascript:alert(1)``,
persisted a proposal (prod id 1837c32c-…) and answered "Proposal filed ✓".
The URL never parsed, so pre-flight degraded to "unknown" and the flow fell
through to persist + dispatch. A repo URL that is not
``https://github.com/<owner>/<repo>`` must be rejected BEFORE pre-flight, the
rate limiter or the DB, with a 422 whose ``detail.detail`` is a sentence a
person can read (BRK-2: the form showed a blank "Not filed:").

C24 / BRK-14: /api/auth/{google,github}/login stored any ``next=`` (e.g.
``https://evil.example``, ``//evil.example``) in the ``oauth_next`` cookie.
The callback already ignored it (SAFE_NEXT_PREFIXES), but the login leg must
apply the SAME check and never store an off-site value.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import app.feedback_ratelimit as rl_module
from app.mcp.tools.federation_propose import loopskill_propose_registry
from app.services.federation_propose import PreflightResult, repo_url_problem

JUNK_URLS = [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "http://github.com/example-org/example-skills",  # https only
    "https://gitlab.com/example-org/example-skills",
    "https://github.com/example-org",  # no repo segment
    "https://github.com/example-org/example skills",  # whitespace inside
    " https://github.com/example-org/example-skills\n",  # surrounding whitespace
    "https://github.com/example-org/example-skills\tx",
    "https://evil.example/?u=https://github.com/a/b",
    "https://github.com/" + "a" * 100 + "/" + "b" * 100,  # > 200 chars
    "example-org/example-skills",  # shorthand was never accepted
    "",
]

GOOD_URLS = [
    "https://github.com/example-org/example-skills",
    "https://github.com/example-org/example-skills.git",
    "https://github.com/example-org/example-skills/",
    "https://github.com/example-org/example-skills/tree/main/skills",
]


@pytest.fixture(autouse=True)
def reset_ratelimit():
    rl_module.reset_all()
    yield
    rl_module.reset_all()


@pytest.mark.parametrize("url", JUNK_URLS)
def test_repo_url_problem_rejects_junk(url):
    problem = repo_url_problem(url)
    assert problem, f"{url!r} must be rejected"
    # A sentence for a person, not an error code.
    assert " " in problem and problem[0].isupper()


@pytest.mark.parametrize("url", GOOD_URLS)
def test_repo_url_problem_accepts_github_repo_urls(url):
    assert repo_url_problem(url) is None


@pytest.mark.parametrize("url", JUNK_URLS)
def test_tool_rejects_junk_before_preflight_or_db(db_session, url):
    from app.models import FederationRegistryProposal as FederationProposal

    before = db_session.query(FederationProposal).count()
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo") as pf,
        patch("app.mcp.tools.federation_propose.github_dispatch.dispatch_event") as disp,
    ):
        result = loopskill_propose_registry(db_session, repo_url=url, why="junk")
    assert result["ok"] is False
    assert result["error"] == "invalid_repo_url"
    assert result["detail"]
    pf.assert_not_called()
    disp.assert_not_called()
    assert db_session.query(FederationProposal).count() == before


def _client(db_session):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.federation_propose_routes import router as federation_router

    test_app = FastAPI()
    test_app.include_router(federation_router)

    def override_db():
        yield db_session

    test_app.dependency_overrides[get_db] = override_db
    return TestClient(test_app, raise_server_exceptions=True)


def test_rest_rejects_javascript_url_with_human_422(db_session):
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo") as pf,
        _client(db_session) as c,
    ):
        resp = c.post("/api/federation/propose", json={"repo_url": "javascript:alert(1)", "why": "junk"})
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["ok"] is False
    assert detail["error"] == "invalid_repo_url"
    assert "https://github.com/" in detail["detail"]
    pf.assert_not_called()


def test_rest_too_long_why_gets_human_message_not_pydantic_list(db_session):
    """BRK-2: a >2048-char why answered a pydantic list the form rendered as
    a blank "Not filed:". It must carry a readable sentence instead."""
    with _client(db_session) as c:
        resp = c.post(
            "/api/federation/propose",
            json={"repo_url": "https://github.com/example-org/example-skills", "why": "x" * 3000},
        )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["error"] == "invalid_input"
    assert "2048" in detail["detail"]


def test_rest_valid_url_still_files(db_session):
    viable = PreflightResult(
        repo_slug="example-org/example-skills",
        repo_exists=True,
        skill_md_count=3,
        license_detected="mit",
        default_branch="main",
    )
    with (
        patch("app.mcp.tools.federation_propose.preflight_repo", return_value=viable),
        patch("app.mcp.tools.federation_propose.github_dispatch.dispatch_event", return_value=True),
        _client(db_session) as c,
    ):
        resp = c.post(
            "/api/federation/propose",
            json={"repo_url": "https://github.com/example-org/example-skills"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["repo_slug"] == "example-org/example-skills"


# ── C24: OAuth login next= ────────────────────────────────────────────────

UNSAFE_NEXT = [
    "https://evil.example",
    "//evil.example",
    "/\\evil.example",
    "/library\\..\\@evil.example",
    "javascript:alert(1)",
    "/nowhere-safe",
    "library",
    "/library\nSet-Cookie: x=1",
]
SAFE_NEXT = ["/library", "/library?tab=fleets", "/skills/copywriting", "/billing/success", "/dashboard"]


@pytest.mark.parametrize("value", UNSAFE_NEXT)
def test_is_safe_next_rejects(value):
    from app.services.safe_next import is_safe_next

    assert is_safe_next(value) is False


@pytest.mark.parametrize("value", SAFE_NEXT)
def test_is_safe_next_accepts(value):
    from app.services.safe_next import is_safe_next

    assert is_safe_next(value) is True


@pytest.fixture()
def oauth_client(db_session, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings
    from tests._app_factory import build_test_app

    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "test-google-id")
    monkeypatch.setattr(settings, "GITHUB_CLIENT_ID", "test-github-id")
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, follow_redirects=False)


def _oauth_next_cookie(resp) -> str | None:
    for header in resp.headers.get_list("set-cookie"):
        if header.startswith("oauth_next="):
            return header
    return None


@pytest.mark.parametrize("provider", ["google", "github"])
@pytest.mark.parametrize("value", ["https://evil.example", "//evil.example", "/\\evil.example"])
def test_login_does_not_store_unsafe_next(oauth_client, provider, value):
    resp = oauth_client.get(f"/api/auth/{provider}/login", params={"next": value})
    assert resp.status_code == 302, resp.text
    assert _oauth_next_cookie(resp) is None


@pytest.mark.parametrize("provider", ["google", "github"])
def test_login_still_stores_safe_next(oauth_client, provider):
    resp = oauth_client.get(f"/api/auth/{provider}/login", params={"next": "/library?tab=fleets"})
    assert resp.status_code == 302, resp.text
    cookie = _oauth_next_cookie(resp)
    assert cookie is not None and "/library" in cookie


def test_success_redirect_uses_same_check():
    from app.auth_routes import _make_success_redirect

    assert _make_success_redirect("jwt", "//evil.example").headers["location"] == "/library?auth=success"
    assert _make_success_redirect("jwt", "/skills/x").headers["location"] == "/skills/x?auth=success"


# ── C01 / C17: the served meta-skill (/skill) ─────────────────────────────

_SKILL_MD = Path(__file__).resolve().parent.parent / "docs" / "recipes-skill" / "SKILL.md"


def _frontmatter_and_body() -> tuple[dict, str]:
    import yaml

    text = _SKILL_MD.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    fm, body = text[4:].split("\n---\n", 1)
    return yaml.safe_load(fm), body


def test_skill_frontmatter_does_not_require_a_key():
    """C01 (IMP-2/SKP-08/MOB-2/NNS-1/BRK-7): the key is OPTIONAL."""
    fm, _ = _frontmatter_and_body()
    assert fm["requiredEnv"] == []
    assert "LOOPSKILL_API_KEY" in fm["optionalEnv"]


def test_skill_opens_with_the_keyless_promise_and_permission_summary():
    _, body = _frontmatter_and_body()
    head = body[:1500]
    assert "Free skills and public bundles install with no" in head
    assert "only needed for the MCP tools" in head
    assert "## What this skill lets your agent do" in head
    assert "**Network:**" in head and "app.loopskill.io" in head
    assert "**Filesystem:**" in head


def test_skill_documents_anonymous_skill_and_bundle_install():
    """The portal phrase ends 'use it to install the "<slug>" bundle|skill'."""
    _, body = _frontmatter_and_body()
    assert 'install the "<slug>" skill' in body
    assert 'install the "<slug>" bundle' in body
    assert "/api/skills/install?slug=<slug>" in body
    assert "/api/bundles/public/<slug>/.well-known/skills/index.json" in body
    assert "installable" in body and "skip_reason" in body


def test_skill_never_asks_to_relay_server_text_verbatim():
    """C17 / SKP-10: no verbatim relay of server-controlled text."""
    _, body = _frontmatter_and_body()
    assert "verbatim" not in body.lower()
    assert "Relay the `account_hint`" not in body
    assert "https://app.loopskill.io/signin" in body
