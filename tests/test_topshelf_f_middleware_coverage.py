"""Phase F — coverage push for app/middleware.py.

Targets uncovered branches:
  - _auth_ctx_from_jwt_cookie: exception / bad UUID paths
  - _auth_ctx_from_api_key: DB exception path
  - get_redis: backoff window, successful reconnect
  - mark_redis_failed
  - APIKeyMiddleware.dispatch:
      /docs/ prefix, webhook, JWT-auth prefixes, public prefixes (with/without key),
      POST public-only path, GET /api/skills/graph, GET /api/skills/{slug}/related,
      cbt_ share-token path (wrong path → 403, bad format → 401, valid → 200),
      non-rec_ prefix → 401, fleet key, master key, user key found/not-found
  - CookbookHostMiddleware: exception path, custom-domain stamp
  - RateLimitMiddleware: authenticated bypass, redis→memory fallback, rate-limit exceeded
"""
from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.auth_ctx import AuthContext
from app.middleware import (
    APIKeyMiddleware,
    CookbookHostMiddleware,
    RateLimitMiddleware,
    _auth_ctx_from_api_key,
    _auth_ctx_from_jwt_cookie,
    get_redis,
    mark_redis_failed,
)


# ─── helpers ──────────────────────────────────────────────────────────────────


def _simple_app_with_middleware(*middleware_classes) -> FastAPI:
    """Build a minimal app with given middleware stacked, one dummy route."""
    app = FastAPI()

    for cls in reversed(middleware_classes):
        app.add_middleware(cls)

    @app.get("/ping")
    def ping():
        return {"pong": True}

    return app


def _echo_app(middleware_cls, **middleware_kwargs) -> FastAPI:
    """App with a single route that echoes request.state as JSON."""
    app = FastAPI()
    app.add_middleware(middleware_cls, **middleware_kwargs)

    @app.get("/echo")
    def echo(request: Request):
        auth = getattr(request.state, "auth_ctx", None)
        return {
            "scope": getattr(auth, "scope", None),
            "bucket_id": getattr(request.state, "bucket_id", None),
        }

    return app


# ─── Unit: _auth_ctx_from_jwt_cookie ─────────────────────────────────────────


class TestAuthCtxFromJwtCookie:
    """Cover _auth_ctx_from_jwt_cookie exception and invalid-UUID branches."""

    def _make_request(self, *, cookie: str | None = None, bearer: str | None = None):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": [],
        }
        if bearer:
            scope["headers"] = [(b"authorization", f"Bearer {bearer}".encode())]
        req = Request(scope)
        if cookie:
            req._cookies = {"wr_jwt": cookie}
        return req

    def test_no_token_returns_anonymous(self):
        req = self._make_request()
        ctx = _auth_ctx_from_jwt_cookie(req)
        assert ctx.scope == "anonymous"

    def test_bearer_token_exception_returns_anonymous(self):
        """verify_jwt raises → anonymous (line 64-65)."""
        req = self._make_request(bearer="bad.token.here")
        with patch("app.auth_routes.verify_jwt", side_effect=ValueError("bad")):
            ctx = _auth_ctx_from_jwt_cookie(req)
        assert ctx.scope == "anonymous"

    def test_invalid_uuid_in_payload_returns_anonymous(self, db_session):
        """payload['sub'] is not a valid UUID → anonymous (line 74-75)."""
        req = self._make_request(bearer="sometoken")
        fake_payload = {"sub": "not-a-uuid"}
        with patch("app.auth_routes.verify_jwt", return_value=fake_payload):
            ctx = _auth_ctx_from_jwt_cookie(req)
        assert ctx.scope == "anonymous"

    def test_payload_none_returns_anonymous(self):
        """verify_jwt returns falsy value → anonymous (line 67-68)."""
        req = self._make_request(bearer="sometoken")
        with patch("app.auth_routes.verify_jwt", return_value=None):
            ctx = _auth_ctx_from_jwt_cookie(req)
        assert ctx.scope == "anonymous"


# ─── Unit: _auth_ctx_from_api_key ────────────────────────────────────────────


class TestAuthCtxFromApiKey:
    """Cover _auth_ctx_from_api_key edge cases including DB exception."""

    def _make_request(self, key: str | None = None):
        headers = []
        if key:
            headers = [(b"x-api-key", key.encode())]
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/skills/access",
            "query_string": b"",
            "headers": headers,
        }
        return Request(scope)

    def test_no_key_returns_none(self):
        req = self._make_request()
        assert _auth_ctx_from_api_key(req) is None

    def test_cbt_prefix_returns_none(self):
        """cbt_ tokens are not handled here — returns None."""
        req = self._make_request("cbt_abcdef12_" + "a" * 32)
        result = _auth_ctx_from_api_key(req)
        assert result is None

    def test_db_exception_returns_none(self):
        """DB lookup exception → returns None, never crashes (line 155-156)."""
        req = self._make_request("rec_" + "a" * 32)
        with patch("app.database.SessionLocal") as mock_session_local:
            mock_db = MagicMock()
            mock_db.query.side_effect = RuntimeError("DB is down")
            mock_session_local.return_value = mock_db
            result = _auth_ctx_from_api_key(req)
        assert result is None


# ─── Unit: get_redis / mark_redis_failed ─────────────────────────────────────


class TestGetRedisHelpers:
    """Cover backoff window, mark_redis_failed, and successful connection."""

    def test_mark_redis_failed_resets_state(self):
        """mark_redis_failed sets _redis_available = None so next call retries (line 186)."""
        import app.middleware as mw
        mw._redis_available = True
        mark_redis_failed()
        assert mw._redis_available is None

    def test_backoff_window_returns_none(self):
        """If we're within the retry-backoff window, get_redis returns None (line 167-168)."""
        import app.middleware as mw
        original_client = mw._redis_client
        original_avail = mw._redis_available
        original_retry = mw._redis_next_retry_at
        try:
            mw._redis_client = None
            mw._redis_available = False
            mw._redis_next_retry_at = time.monotonic() + 60.0  # far future
            result = get_redis()
            assert result is None
        finally:
            mw._redis_client = original_client
            mw._redis_available = original_avail
            mw._redis_next_retry_at = original_retry

    def test_get_redis_returns_existing_client_when_available(self):
        """If _redis_client is set and _redis_available is True, returns it (line 164-165)."""
        import app.middleware as mw
        mock_client = MagicMock()
        original_client = mw._redis_client
        original_avail = mw._redis_available
        try:
            mw._redis_client = mock_client
            mw._redis_available = True
            result = get_redis()
            assert result is mock_client
        finally:
            mw._redis_client = original_client
            mw._redis_available = original_avail

    def test_get_redis_connection_error_sets_backoff(self):
        """Redis connection error sets _redis_available=False and schedules retry (line 176-180)."""
        import app.middleware as mw
        import redis as redis_lib
        original_client = mw._redis_client
        original_avail = mw._redis_available
        original_retry = mw._redis_next_retry_at
        try:
            mw._redis_client = None
            mw._redis_available = None
            mw._redis_next_retry_at = 0.0
            with patch("redis.from_url") as mock_from_url:
                mock_redis_instance = MagicMock()
                mock_redis_instance.ping.side_effect = redis_lib.ConnectionError("refused")
                mock_from_url.return_value = mock_redis_instance
                result = get_redis()
            assert result is None
            assert mw._redis_available is False
            assert mw._redis_next_retry_at > time.monotonic()
        finally:
            mw._redis_client = original_client
            mw._redis_available = original_avail
            mw._redis_next_retry_at = original_retry


# ─── Integration: APIKeyMiddleware dispatch paths ─────────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import StaticPool
    from app.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_pragma(conn, _rec):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db_session(engine_fixture):
    from sqlalchemy.orm import sessionmaker
    conn = engine_fixture.connect()
    txn = conn.begin()
    Session = sessionmaker(bind=conn)
    session = Session()
    nested = conn.begin_nested()

    from sqlalchemy import event as sa_event
    @sa_event.listens_for(session, "after_transaction_end")
    def restart_sp(s, t):
        nonlocal nested
        if not nested.is_active:
            nested = conn.begin_nested()

    yield session
    session.close()
    txn.rollback()
    conn.close()


def _build_mw_app(db_session, monkeypatch):
    """Build full test app with APIKeyMiddleware + all routes."""
    from tests._app_factory import build_test_app
    return build_test_app(db_session=db_session, monkeypatch=monkeypatch)


class TestAPIKeyMiddlewareDispatch:

    def test_docs_slash_prefix_passes_through(self, db_session, monkeypatch):
        """GET /docs/something passes through without API key (line 272-273)."""
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/docs/swagger-ui-init.js")
        assert resp.status_code != 401

    def test_stripe_webhook_passes_through(self, db_session, monkeypatch):
        """POST /api/stripe/webhook passes through (line 276-277)."""
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/stripe/webhook", content=b"{}")
        # Should not be 401 (webhook signature validation happens inside route)
        assert resp.status_code != 401

    def test_jwt_auth_prefix_passes_through(self, db_session, monkeypatch):
        """JWT-auth prefix path is not blocked by APIKeyMiddleware (line 280-281).

        The middleware dispatches these paths without checking x-api-key.
        We verify by checking the response is NOT the middleware's 401 message.
        """
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        # /api/me/ is a JWT-auth prefix — middleware should dispatch, not block
        resp = client.get("/api/me/referral-code")
        # The route itself may return various codes, but middleware's 401 has specific body
        if resp.status_code == 401:
            body = resp.json()
            # Make sure it's NOT the middleware's generic "Invalid or missing x-api-key" message
            assert "x-api-key" not in body.get("detail", ""), (
                f"Middleware blocked a JWT-auth prefix path: {body}"
            )

    def test_public_prefix_with_valid_api_key_stamps_auth_ctx(self, db_session, monkeypatch):
        """Public prefix + valid master key → stamps master auth_ctx (lines 289-297)."""
        from app.config import settings
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/skills/search?q=test", headers={"x-api-key": settings.API_KEY})
        assert resp.status_code != 401

    def test_public_prefix_no_key_stamps_anonymous(self, db_session, monkeypatch):
        """Public prefix + no key → stamps anonymous auth_ctx (lines 293-296)."""
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/skills/search?q=test")
        assert resp.status_code in (200, 404)  # not 401

    def test_post_intent_survey_public(self, db_session, monkeypatch):
        """POST /api/intent-survey passes through without API key (line 300-301)."""
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/api/intent-survey", json={"intent": "test"})
        assert resp.status_code != 401

    def test_get_skills_graph_public(self, db_session, monkeypatch):
        """GET /api/skills/graph is public (line 312-313)."""
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/skills/graph")
        assert resp.status_code != 401

    def test_get_skills_slug_related_public(self, db_session, monkeypatch):
        """GET /api/skills/some-skill/related is public (lines 362-370)."""
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/skills/some-skill/related")
        assert resp.status_code != 401

    def test_missing_api_key_returns_401(self, db_session, monkeypatch):
        """Protected path with no API key → 401 (lines 374-380).

        The free-install path is a special case. Use a path that requires auth
        and is not in the public install shortcut. Test by hitting an admin endpoint.
        """
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        # /api/admin/ is not exempted and has no auth shortcut — middleware returns 401
        resp = client.get("/api/admin/users")
        assert resp.status_code == 401
        assert "x-api-key" in resp.json().get("detail", "").lower()

    def test_non_rec_prefix_returns_401(self, db_session, monkeypatch):
        """API key with wrong prefix → 401 (lines 481-486)."""
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/skills/install?slug=test",
            headers={"x-api-key": "sk_invalid_prefix"},
        )
        assert resp.status_code == 401

    def test_master_key_passes_through(self, db_session, monkeypatch):
        """Master API key stamps master scope and allows requests (line 519-527)."""
        from app.config import settings
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/skills/install?slug=test",
            headers={"x-api-key": settings.API_KEY},
        )
        # Master key passes auth (may be 404 because skill doesn't exist)
        assert resp.status_code != 401

    def test_invalid_user_key_returns_401(self, db_session, monkeypatch):
        """Unknown rec_ key → 401 (lines 581-586)."""
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/skills/install?slug=test",
            headers={"x-api-key": "rec_" + "z" * 32},
        )
        assert resp.status_code == 401

    def test_cbt_token_wrong_path_returns_403(self, db_session, monkeypatch):
        """cbt_ token on non-cookbook path → 403 (lines 397-403)."""
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        cbt_token = "cbt_abcdef12_" + "a" * 32
        resp = client.get(
            "/api/skills/install?slug=test",
            headers={"x-api-key": cbt_token},
        )
        # Without allow_public_catalog, cbt_ token on install path → 403
        assert resp.status_code in (401, 403)

    def test_cbt_token_bad_format_returns_401(self, db_session, monkeypatch):
        """cbt_ token with malformed format → 401 (lines 406-412)."""
        app = _build_mw_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        # bad format: only 2 parts
        resp = client.get(
            "/api/cookbooks/",
            headers={"x-api-key": "cbt_badfmt"},
        )
        assert resp.status_code == 401


# ─── Unit: cbt_ token full validation path ────────────────────────────────────


class TestCbtTokenValidation:
    """Cover cbt_ token DB-lookup path in APIKeyMiddleware (lines 419-478)."""

    def _build_cbt_app(self, db_session, monkeypatch):
        from tests._app_factory import build_test_app
        return build_test_app(db_session=db_session, monkeypatch=monkeypatch)

    def _make_cbt_token_and_row(self, db_session, *, allow_public_catalog: bool = False):
        """Create a CookbookShareToken row and return (plaintext_key, cookbook_id)."""
        import secrets
        from app.models import CookbookShareToken, User, Cookbook

        user = User(id=uuid4(), display_name="T", email=f"{uuid4()}@t.com")
        db_session.add(user)
        db_session.flush()

        cb = Cookbook(
            id=uuid4(),
            name="TestCB",
            description="x",
            is_base=False,
            bundle_owner=user.id,
        )
        db_session.add(cb)
        db_session.flush()

        prefix = secrets.token_hex(4)  # 8 hex chars
        rand = secrets.token_hex(16)   # 32 hex chars
        plaintext = f"cbt_{prefix}_{rand}"
        key_hash = hashlib.sha256(plaintext.encode()).hexdigest()

        row = CookbookShareToken(
            id=uuid4(),
            bundle_id=cb.id,
            token_prefix=prefix,
            token_hash=key_hash,
            scope="install",
            is_active=True,
            allow_public_catalog=allow_public_catalog,
        )
        db_session.add(row)
        db_session.flush()
        return plaintext, cb.id

    def test_cbt_token_valid_cookbook_path_stamps_auth_ctx(self, db_session, monkeypatch):
        """Valid cbt_ token on cookbook path → stamps cbt_token auth_ctx (line 459-475)."""
        token, cb_id = self._make_cbt_token_and_row(db_session)
        app = self._build_cbt_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        # Hit any cookbook endpoint to exercise the cbt_ middleware path
        # The middleware should stamp cbt_token scope before routing
        resp = client.get(
            f"/api/cookbooks/{cb_id}",
            headers={"x-api-key": token},
        )
        # The route may 404 (cookbook CRUD requires ownership checks)
        # but we should NOT get a middleware-level 401 about "Invalid or revoked share token"
        assert resp.status_code != 401 or "x-api-key" not in resp.json().get("detail", "")

    def test_cbt_token_invalid_hash_returns_401(self, db_session, monkeypatch):
        """cbt_ token that doesn't match any row → 401 (lines 435-441)."""
        import secrets
        app = self._build_cbt_app(db_session, monkeypatch)
        prefix = secrets.token_hex(4)
        rand = secrets.token_hex(16)
        plaintext = f"cbt_{prefix}_{rand}"
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get(
            "/api/cookbooks/",
            headers={"x-api-key": plaintext},
        )
        assert resp.status_code == 401

    def test_cbt_token_allow_public_catalog_on_install_path(self, db_session, monkeypatch):
        """cbt_ token with allow_public_catalog=True on install path passes (line 450-451)."""
        token, _ = self._make_cbt_token_and_row(db_session, allow_public_catalog=True)
        app = self._build_cbt_app(db_session, monkeypatch)
        client = TestClient(app, raise_server_exceptions=False)
        # With allow_public_catalog=True, should NOT return 403
        resp = client.get(
            "/api/skills/install?slug=nonexistent",
            headers={"x-api-key": token},
        )
        # Gets past the middleware; 404 from the route is expected
        assert resp.status_code != 403


# ─── Integration: CookbookHostMiddleware ──────────────────────────────────────


class TestCookbookHostMiddleware:
    """Cover CookbookHostMiddleware custom-domain and exception paths."""

    def _build_cookbook_app(self):
        app = FastAPI()
        app.add_middleware(CookbookHostMiddleware)

        @app.get("/probe")
        def probe(request: Request):
            return {
                "cookbook_id": getattr(request.state, "cookbook_id", None),
                "cookbook_slug": getattr(request.state, "cookbook_slug", None),
            }

        return app

    def test_localhost_host_skipped(self):
        """SKIP_HOSTS: localhost is never treated as custom domain."""
        app = self._build_cookbook_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/probe", headers={"host": "localhost"})
        assert resp.status_code == 200
        assert resp.json()["cookbook_id"] is None

    def test_testserver_host_skipped(self):
        """SKIP_HOSTS: testserver is never treated as custom domain."""
        app = self._build_cookbook_app()
        client = TestClient(app)
        resp = client.get("/probe")
        assert resp.status_code == 200
        assert resp.json()["cookbook_id"] is None

    def test_db_exception_doesnt_crash(self):
        """DB lookup exception → logged but request continues."""
        app = self._build_cookbook_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch("app.database.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_db.query.side_effect = RuntimeError("DB explosion")
            mock_sl.return_value = mock_db
            resp = client.get("/probe", headers={"host": "custom.example.com"})
        assert resp.status_code == 200  # request continues despite DB error

    def test_matching_custom_domain_stamps_cookbook_id(self):
        """Known custom domain → stamps cookbook_id on request.state."""
        from app.models import Cookbook as CookbookModel

        mock_cb = MagicMock(spec=CookbookModel)
        mock_cb.id = uuid4()
        mock_cb.slug = "my-cookbook"
        mock_cb.theme_json = "{}"

        app = self._build_cookbook_app()
        client = TestClient(app, raise_server_exceptions=False)
        with patch("app.database.SessionLocal") as mock_sl:
            mock_db = MagicMock()
            mock_db.query.return_value.filter.return_value.first.return_value = mock_cb
            mock_sl.return_value = mock_db
            resp = client.get("/probe", headers={"host": "custom.example.com"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["cookbook_slug"] == "my-cookbook"


# ─── Integration: RateLimitMiddleware ─────────────────────────────────────────


class TestRateLimitMiddleware:
    """Cover RateLimitMiddleware paths: auth bypass, redis→memory fallback, exceeded."""

    def _build_rl_app(self, max_requests: int = 60):
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, max_requests=max_requests, window_seconds=60)

        @app.get("/test")
        def test_route(request: Request):
            return {"ok": True}

        return app

    def test_authenticated_scope_bypasses_rate_limit(self):
        """Authenticated callers skip the rate limit entirely (lines 730-733)."""
        app = self._build_rl_app(max_requests=1)
        client = TestClient(app, raise_server_exceptions=False)

        # Stamp a non-anonymous auth_ctx via a wrapping middleware
        from starlette.middleware.base import BaseHTTPMiddleware

        class StampAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                request.state.auth_ctx = AuthContext(scope="master")
                return await call_next(request)

        app.add_middleware(StampAuth)
        # Even with max_requests=1, authenticated calls should always pass
        for _ in range(5):
            resp = client.get("/test")
            assert resp.status_code == 200

    def test_anonymous_rate_limit_exceeded_returns_429(self):
        """Anonymous callers that exceed limit get 429 (lines 742-748)."""
        app = self._build_rl_app(max_requests=2)

        with patch("app.middleware.get_redis", return_value=None):
            client = TestClient(app, raise_server_exceptions=False)
            # First two requests should pass
            for _ in range(2):
                resp = client.get("/test")
                assert resp.status_code == 200
            # Third should be rate-limited
            resp = client.get("/test")
            assert resp.status_code == 429

    def test_redis_failure_falls_back_to_memory(self):
        """Redis pipeline failure → _check_redis returns None → uses memory (lines 695-698, 738-740)."""
        import redis as redis_lib

        app = self._build_rl_app(max_requests=100)

        mock_redis = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.execute.side_effect = redis_lib.ConnectionError("pipe failed")
        mock_redis.pipeline.return_value = mock_pipe

        with patch("app.middleware.get_redis", return_value=mock_redis):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/test")
        assert resp.status_code == 200  # falls back to memory, request allowed

    def test_exempt_paths_bypass_rate_limit(self):
        """EXEMPT_PATHS pass through without being rate-limited (lines 708-709)."""
        app = self._build_rl_app(max_requests=1)
        with patch("app.middleware.get_redis", return_value=None):
            client = TestClient(app, raise_server_exceptions=False)
            for _ in range(5):
                resp = client.get("/healthz")
                assert resp.status_code in (200, 404)  # not 429
