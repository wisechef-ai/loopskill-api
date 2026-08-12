"""Tests for money-path-3: first-touch UTM/ref attribution capture at signup.

2026-08-12 money-path audit, Fix #3. Covers the pure resolver
(``app.services.signup_attribution.resolve_signup_attribution``) at unit
level, AND the real end-to-end signup path through
``GET /api/auth/github/callback`` (house rule: route input through the real
request path, no raw-SQL/direct-attribute seeding proving nothing).

Test list:
  (a) signup with ref cookie only            -> attribution persisted, ref set
  (b) signup with utm query params only      -> attribution persisted
  (c) signup with both cookie ctx AND query  -> cookie wins (first-touch)
  (d) second login (existing user)           -> does NOT overwrite
  (e) oversized / control-char utm values    -> safely bounded, never stored raw
  (f) signup with neither cookie nor query   -> null attribution, no error
  (g) cookies blocked (client sends none)    -> signup still succeeds (never a blocker)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.services.signup_attribution import (
    _UTM_CTX_COOKIE_NAME,
    _UTM_FIELD_MAX_LEN,
    resolve_signup_attribution,
)


# ── Unit-level tests: resolve_signup_attribution() against a fake Request ──


class _FakeRequest:
    """Minimal stand-in for fastapi.Request — only .cookies / .query_params used."""

    def __init__(self, cookies: dict | None = None, query_params: dict | None = None):
        self.cookies = cookies or {}
        self.query_params = query_params or {}


class TestResolveSignupAttributionUnit:
    def test_cookie_ref_only(self):
        """(a) ref cookie present, no UTM ctx cookie, no query -> ref captured."""
        req = _FakeRequest(cookies={"recipes_utm_ref": "li"})
        result = resolve_signup_attribution(req)
        assert result is not None
        assert result["ref"] == "li"
        assert result["utm_source"] is None
        assert "captured_at" in result

    def test_query_utm_only(self):
        """(b) no cookies, UTM query params present -> attribution persisted."""
        req = _FakeRequest(
            query_params={
                "utm_source": "twitter",
                "utm_medium": "social",
                "utm_campaign": "launch_week",
                "utm_content": "banner_a",
            }
        )
        result = resolve_signup_attribution(req)
        assert result is not None
        assert result["utm_source"] == "twitter"
        assert result["utm_medium"] == "social"
        assert result["utm_campaign"] == "launch_week"
        assert result["utm_content"] == "banner_a"
        assert result["ref"] is None

    def test_cookie_ctx_wins_over_query(self):
        """(c) both UTM ctx cookie AND query params present -> cookie wins."""
        ctx = json.dumps({"utm_source": "cookie_source", "utm_medium": "cookie_medium"})
        req = _FakeRequest(
            cookies={_UTM_CTX_COOKIE_NAME: ctx},
            query_params={"utm_source": "query_source", "utm_medium": "query_medium"},
        )
        result = resolve_signup_attribution(req)
        assert result["utm_source"] == "cookie_source"
        assert result["utm_medium"] == "cookie_medium"
        # never falls back to the query values when the cookie already won
        assert result["utm_source"] != "query_source"

    def test_oversized_and_control_char_values_bounded(self):
        """(e) oversized value truncated; control-char value dropped, not stored raw."""
        oversized = "x" * 500
        control_char = "evil\x00payload"
        req = _FakeRequest(
            query_params={
                "utm_source": oversized,
                "utm_medium": control_char,
                "utm_campaign": "clean_value",
            }
        )
        result = resolve_signup_attribution(req)
        assert result is not None
        assert len(result["utm_source"]) == _UTM_FIELD_MAX_LEN
        assert result["utm_source"] == oversized[:_UTM_FIELD_MAX_LEN]
        assert result["utm_medium"] is None  # control-char value rejected outright
        assert result["utm_campaign"] == "clean_value"

    def test_ref_cookie_unknown_value_rejected(self):
        """(e) a ref cookie value outside the allowlist/creator: shape is dropped."""
        req = _FakeRequest(cookies={"recipes_utm_ref": "totally-made-up-platform"})
        result = resolve_signup_attribution(req)
        assert result is None  # nothing else to attribute either

    def test_ref_cookie_oversized_rejected(self):
        """(e) an absurdly long ref cookie is rejected before any further processing."""
        req = _FakeRequest(cookies={"recipes_utm_ref": "x" * 1000})
        result = resolve_signup_attribution(req)
        assert result is None

    def test_creator_namespaced_ref_accepted(self):
        """A pre-validated creator:<handle> ref cookie (set by _set_utm_ref_cookie
        at an earlier visit) passes through unchanged."""
        req = _FakeRequest(cookies={"recipes_utm_ref": "creator:somecreator"})
        result = resolve_signup_attribution(req)
        assert result["ref"] == "creator:somecreator"

    def test_no_signal_returns_none(self):
        """(f) nothing set anywhere -> None, no exception."""
        req = _FakeRequest()
        result = resolve_signup_attribution(req)
        assert result is None

    def test_malformed_ctx_cookie_json_falls_through(self):
        """Garbage JSON in the ctx cookie must not raise; falls through to query."""
        req = _FakeRequest(
            cookies={_UTM_CTX_COOKIE_NAME: "{not valid json"},
            query_params={"utm_source": "fallback_source"},
        )
        result = resolve_signup_attribution(req)
        assert result is not None
        assert result["utm_source"] == "fallback_source"


# ── Integration tests: real OAuth callback route, real DB write path ───────


@pytest.fixture()
def auth_client(db_session: Session):
    """TestClient wired to the real /api/auth/github/callback route.

    House rule: route input through the real request path — no raw-SQL /
    direct-attribute seeding of signup_attribution proving nothing. This
    fixture only mocks the external GitHub HTTP exchange (exchange_github_code);
    everything downstream (find_or_create_user_by_github, the attribution
    capture, the DB write) runs for real against the sqlite test session.
    """
    from app.config import settings as real_settings

    test_app = FastAPI()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    from app.auth_routes import router as auth_router

    test_app.include_router(auth_router)
    test_app.dependency_overrides[get_db] = override_get_db

    with (
        patch.object(real_settings, "GITHUB_CLIENT_ID", "test_gh_id"),
        patch.object(real_settings, "GITHUB_CLIENT_SECRET", "test_gh_secret"),
    ):
        with TestClient(test_app, raise_server_exceptions=True) as c:
            yield c


def _do_github_callback(
    auth_client,
    *,
    github_id: int,
    cookies: dict | None = None,
    query: str = "",
):
    """Drive the real GitHub OAuth callback: state-cookie handshake + mocked exchange."""
    state = "test-state-abc"
    all_cookies = {"oauth_state": state}
    all_cookies.update(cookies or {})
    for k, v in all_cookies.items():
        auth_client.cookies.set(k, v)

    fake_github_data = {
        "provider": "github",
        "github_id": github_id,
        "username": f"user{github_id}",
        "display_name": f"Test User {github_id}",
        "email": f"user{github_id}@example.com",
        "avatar_url": None,
    }

    with patch("app.auth_routes.exchange_github_code", new=AsyncMock(return_value=fake_github_data)):
        url = f"/api/auth/github/callback?code=abc&state={state}"
        if query:
            url += f"&{query}"
        resp = auth_client.get(url, follow_redirects=False)
    return resp


class TestSignupAttributionEndToEnd:
    def test_ref_cookie_persisted_on_signup(self, auth_client, db_session):
        """(a) signup with ref cookie -> attribution persisted."""
        resp = _do_github_callback(auth_client, github_id=90001, cookies={"recipes_utm_ref": "li"})
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90001).first()
        assert user is not None
        assert user.signup_attribution is not None
        assert user.signup_attribution["ref"] == "li"

    def test_utm_query_only_persisted_on_signup(self, auth_client, db_session):
        """(b) signup with utm query params only -> attribution persisted."""
        resp = _do_github_callback(
            auth_client,
            github_id=90002,
            query="utm_source=x&utm_medium=social&utm_campaign=launch",
        )
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90002).first()
        assert user is not None
        assert user.signup_attribution is not None
        assert user.signup_attribution["utm_source"] == "x"
        assert user.signup_attribution["utm_campaign"] == "launch"
        assert user.signup_attribution["ref"] is None

    def test_cookie_wins_over_query_on_signup(self, auth_client, db_session):
        """(c) both cookie ctx and query present -> cookie wins (first-touch)."""
        ctx = json.dumps({"utm_source": "cookie_wins_source"})
        resp = _do_github_callback(
            auth_client,
            github_id=90003,
            cookies={_UTM_CTX_COOKIE_NAME: ctx},
            query="utm_source=query_loses_source",
        )
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90003).first()
        assert user.signup_attribution["utm_source"] == "cookie_wins_source"

    def test_second_login_does_not_overwrite(self, auth_client, db_session):
        """(d) a returning user's second login must NOT clobber first-touch attribution."""
        # First login: captures ref=li.
        resp1 = _do_github_callback(auth_client, github_id=90004, cookies={"recipes_utm_ref": "li"})
        assert resp1.status_code == 302
        user = db_session.query(User).filter(User.github_id == 90004).first()
        original_attribution = dict(user.signup_attribution)

        # Second login: different ref cookie this time (e.g. clicked a
        # different platform link while already having an account) — must
        # NOT overwrite the original first-touch record.
        resp2 = _do_github_callback(auth_client, github_id=90004, cookies={"recipes_utm_ref": "x"})
        assert resp2.status_code == 302

        db_session.refresh(user)
        assert user.signup_attribution == original_attribution
        assert user.signup_attribution["ref"] == "li"  # unchanged, not "x"

    def test_oversized_garbage_utm_safely_bounded_on_signup(self, auth_client, db_session):
        """(e) oversized/garbage utm on the real signup path -> bounded, not raw-stored."""
        oversized = "y" * 900
        resp = _do_github_callback(
            auth_client,
            github_id=90005,
            query=f"utm_source={oversized}&utm_medium=%00control",
        )
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90005).first()
        assert user.signup_attribution is not None
        assert len(user.signup_attribution["utm_source"]) == _UTM_FIELD_MAX_LEN
        assert user.signup_attribution["utm_medium"] is None

    def test_no_attribution_signal_null_no_error(self, auth_client, db_session):
        """(f) signup with neither cookie nor query -> null attribution, 302 succeeds."""
        resp = _do_github_callback(auth_client, github_id=90006)
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90006).first()
        assert user is not None
        assert user.signup_attribution is None

    def test_signup_succeeds_when_attribution_cookies_blocked(self, auth_client, db_session):
        """(g) adversarial self-review: if the client blocks ALL cookies except the
        mandatory oauth_state (CSRF) cookie the OAuth handshake itself requires,
        signup must still succeed — attribution is best-effort, never a blocker.
        This is the literal 'cookies blocked' scenario: no recipes_utm_ref, no
        recipes_utm_ctx, no referral cookie at all.
        """
        resp = _do_github_callback(auth_client, github_id=90007)  # no attribution cookies
        assert resp.status_code == 302
        assert "auth=success" in resp.headers.get("location", "") or resp.headers.get(
            "location", ""
        ).startswith("/library")

        user = db_session.query(User).filter(User.github_id == 90007).first()
        assert user is not None  # signup completed
        assert user.signup_attribution is None  # nothing to attribute, no crash

    def test_signup_succeeds_when_resolver_raises(self, auth_client, db_session):
        """(g) even if resolve_signup_attribution itself raises unexpectedly, the
        auth_routes.py try/except around the call site must still let signup
        complete — belt-and-suspenders on top of the resolver's own internal
        never-raise design.
        """
        with patch(
            "app.auth_routes.resolve_signup_attribution",
            side_effect=RuntimeError("boom"),
        ):
            resp = _do_github_callback(auth_client, github_id=90008)
        assert resp.status_code == 302

        user = db_session.query(User).filter(User.github_id == 90008).first()
        assert user is not None  # signup still completed despite the raise
        assert user.signup_attribution is None
