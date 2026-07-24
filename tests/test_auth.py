"""Tests for auth module — JWT, GitHub OAuth, Google OAuth, auth routes.

Covers:
- JWT creation, verification, expiry, invalid signatures, wrong issuer, malformed
- GitHub OAuth URL generation, code exchange, user find-or-create
- Google OAuth URL generation, code exchange, user find-or-create
- Auth router endpoints: /github/login, /github/callback, /google/login, /google/callback, /me
"""

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import jwt as pyjwt

from app.auth import (
    AuthError,
    create_jwt,
    verify_jwt,
    get_github_auth_url,
    get_google_auth_url,
    exchange_github_code,
    exchange_google_code,
    find_or_create_user_by_github,
    find_or_create_user_by_google,
    get_user_from_jwt,
)
from app.config import settings
from app.models import User


# ── JWT tests ────────────────────────────────────────────────────────────


class TestJWT:
    """Test JWT creation and verification."""

    def test_create_and_verify_jwt(self):
        """Create a JWT and verify it round-trips correctly."""
        user_id = uuid4()
        user = User(
            id=user_id,
            display_name="Test Creator",
            email="test@example.com",
        )
        token = create_jwt(user)
        assert isinstance(token, str)
        assert len(token) > 20

        payload = verify_jwt(token)
        assert payload is not None
        assert payload["sub"] == str(user_id)
        assert payload["email"] == "test@example.com"
        assert payload["iss"] == "wiserecipes"
        assert "exp" in payload

    def test_expired_jwt_rejected(self):
        """Expired tokens should be rejected."""
        payload = {
            "sub": str(uuid4()),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
            "iat": datetime.now(timezone.utc) - timedelta(hours=73),
            "iss": "wiserecipes",
        }
        token = pyjwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
        result = verify_jwt(token)
        assert result is None

    def test_invalid_signature_rejected(self):
        """Tokens signed with wrong secret should be rejected."""
        payload = {
            "sub": str(uuid4()),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
            "iss": "wiserecipes",
        }
        token = pyjwt.encode(payload, "wrong-secret", algorithm="HS256")
        result = verify_jwt(token)
        assert result is None

    def test_wrong_issuer_rejected(self):
        """Tokens with wrong issuer should be rejected."""
        payload = {
            "sub": str(uuid4()),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            "iat": datetime.now(timezone.utc),
            "iss": "wrong-app",
        }
        token = pyjwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
        result = verify_jwt(token)
        assert result is None

    def test_malformed_token_rejected(self):
        """Garbage tokens should be rejected."""
        assert verify_jwt("not.a.token") is None
        assert verify_jwt("") is None
        assert verify_jwt("eyJhbG...bage") is None


# ── GitHub OAuth helper tests ────────────────────────────────────────────


class TestGitHubAuthURL:
    """Test GitHub OAuth URL generation."""

    def test_github_auth_url_contains_params(self):
        """Generated URL should include client_id, redirect_uri, scope, state."""
        with patch.object(settings, "GITHUB_CLIENT_ID", "test_client_id"):
            url = get_github_auth_url(state="random_state", redirect_uri="http://localhost/cb")
        assert "client_id=test_client_id" in url
        # OAuth helpers URL-encode the redirect_uri per spec, so the literal
        # form may appear as "http%3A%2F%2Flocalhost%2Fcb". Decode the URL
        # query before asserting so this test stays stable against quoting
        # changes.
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        assert qs.get("redirect_uri") == ["http://localhost/cb"]
        assert "state=random_state" in url
        assert "scope=read:user" in url or "scope=read%3Auser" in url
        assert url.startswith("https://github.com/login/oauth/authorize")

    def test_github_auth_url_missing_client_id(self):
        """Should still build URL even with empty client_id (validation happens on exchange)."""
        with patch.object(settings, "GITHUB_CLIENT_ID", ""):
            url = get_github_auth_url(state="s", redirect_uri="http://x/cb")
        assert "client_id=" in url


class TestGitHubCodeExchange:
    """Test GitHub OAuth code exchange."""

    @pytest.mark.asyncio
    async def test_exchange_raises_when_not_configured(self):
        """Should raise AuthError when client ID/secret are missing."""
        with patch.object(settings, "GITHUB_CLIENT_ID", ""), \
             patch.object(settings, "GITHUB_CLIENT_SECRET", ""):
            with pytest.raises(AuthError, match="not configured"):
                await exchange_github_code("fake_code")

    @pytest.mark.asyncio
    async def test_exchange_success(self):
        """Should exchange code for user profile data."""
        mock_response_token = MagicMock()
        mock_response_token.status_code = 200
        mock_response_token.json.return_value = {"access_token": "gh_test_token"}

        mock_response_profile = MagicMock()
        mock_response_profile.status_code = 200
        mock_response_profile.json.return_value = {
            "id": 12345,
            "login": "testuser",
            "name": "Test User",
            "email": "test@example.com",
            "avatar_url": "https://avatars.githubusercontent.com/u/12345",
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response_token
        mock_client.get.return_value = mock_response_profile
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(settings, "GITHUB_CLIENT_ID", "id"), \
             patch.object(settings, "GITHUB_CLIENT_SECRET", "secret"), \
             patch("app.auth.httpx.AsyncClient", return_value=mock_client):
            result = await exchange_github_code("valid_code")

        assert result["provider"] == "github"
        assert result["github_id"] == 12345
        assert result["username"] == "testuser"
        assert result["display_name"] == "Test User"
        assert result["email"] == "test@example.com"

    @pytest.mark.asyncio
    async def test_exchange_fetches_email_if_not_public(self):
        """Should fetch email from /user/emails endpoint when profile has none."""
        mock_response_token = MagicMock()
        mock_response_token.status_code = 200
        mock_response_token.json.return_value = {"access_token": "gh_test_token"}

        mock_response_profile = MagicMock()
        mock_response_profile.status_code = 200
        mock_response_profile.json.return_value = {
            "id": 67890,
            "login": "privatemail",
            "name": None,
            "email": None,
            "avatar_url": None,
        }

        mock_response_emails = MagicMock()
        mock_response_emails.status_code = 200
        mock_response_emails.json.return_value = [
            {"email": "private@example.com", "primary": True, "verified": True},
            {"email": "other@example.com", "primary": False, "verified": True},
        ]

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response_token
        mock_client.get = AsyncMock(side_effect=[mock_response_profile, mock_response_emails])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(settings, "GITHUB_CLIENT_ID", "id"), \
             patch.object(settings, "GITHUB_CLIENT_SECRET", "secret"), \
             patch("app.auth.httpx.AsyncClient", return_value=mock_client):
            result = await exchange_github_code("valid_code")

        assert result["email"] == "private@example.com"
        assert result["display_name"] == "privatemail"  # Falls back to login

    @pytest.mark.asyncio
    async def test_exchange_token_failure(self):
        """Should raise AuthError when token exchange fails."""
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"error": "bad_verification_code"}

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(settings, "GITHUB_CLIENT_ID", "id"), \
             patch.object(settings, "GITHUB_CLIENT_SECRET", "secret"), \
             patch("app.auth.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(AuthError, match="token exchange failed"):
                await exchange_github_code("bad_code")


# ── Google OAuth helper tests ────────────────────────────────────────────


class TestGoogleAuthURL:
    """Test Google OAuth URL generation."""

    def test_google_auth_url_contains_params(self):
        """Generated URL should include client_id, redirect_uri, scope, state."""
        with patch.object(settings, "GOOGLE_CLIENT_ID", "g_test_client"):
            url = get_google_auth_url(state="g_state", redirect_uri="http://localhost/cb")
        assert "client_id=g_test_client" in url
        # URL-encoded redirect_uri (same as GitHub OAuth test).
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(url).query)
        assert qs.get("redirect_uri") == ["http://localhost/cb"]
        assert "state=g_state" in url
        # Google scope is "openid email profile" → URL-encoded as
        # "openid+email+profile" or "openid%20email%20profile".
        scope = qs.get("scope", [""])[0]
        assert "openid" in scope and "email" in scope and "profile" in scope
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")


class TestGoogleCodeExchange:
    """Test Google OAuth code exchange."""

    @pytest.mark.asyncio
    async def test_exchange_raises_when_not_configured(self):
        """Should raise AuthError when Google client ID/secret are missing."""
        with patch.object(settings, "GOOGLE_CLIENT_ID", ""), \
             patch.object(settings, "GOOGLE_CLIENT_SECRET", ""):
            with pytest.raises(AuthError, match="not configured"):
                await exchange_google_code("fake_code")

    @pytest.mark.asyncio
    async def test_exchange_success(self):
        """Should exchange code for user profile data."""
        mock_response_token = MagicMock()
        mock_response_token.status_code = 200
        mock_response_token.json.return_value = {"access_token": "ya29.test_token"}

        mock_response_profile = MagicMock()
        mock_response_profile.status_code = 200
        mock_response_profile.json.return_value = {
            "id": "google_123",
            "name": "G Test User",
            "email": "gtest@example.com",
            "picture": "https://lh3.googleusercontent.com/test",
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response_token
        mock_client.get.return_value = mock_response_profile
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch.object(settings, "GOOGLE_CLIENT_ID", "id"), \
             patch.object(settings, "GOOGLE_CLIENT_SECRET", "secret"), \
             patch("app.auth.httpx.AsyncClient", return_value=mock_client):
            result = await exchange_google_code("valid_code")

        assert result["provider"] == "google"
        assert result["google_id"] == "google_123"
        assert result["display_name"] == "G Test User"
        assert result["email"] == "gtest@example.com"
        assert result["avatar_url"] == "https://lh3.googleusercontent.com/test"


# ── User find-or-create tests ────────────────────────────────────────────


class TestFindOrCreateUser:
    """Test user find-or-create logic for GitHub and Google."""

    def test_find_or_create_github_new_user(self):
        """Should create a new user when GitHub ID not found."""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        github_data = {
            "github_id": 99999,
            "username": "newuser",
            "display_name": "New User",
            "email": "new@example.com",
            "avatar_url": "https://avatars.githubusercontent.com/u/99999",
        }

        user = find_or_create_user_by_github(db, github_data)
        assert user is not None
        assert user.github_id == 99999
        assert user.display_name == "New User"
        assert user.email == "new@example.com"
        db.add.assert_called_once()
        db.commit.assert_called()

    def test_find_or_create_github_existing_user(self):
        """Should update and return existing user when GitHub ID found."""
        existing = User(
            id=uuid4(),
            github_id=11111,
            display_name="Old Name",
            email="old@example.com",
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = existing

        github_data = {
            "github_id": 11111,
            "username": "existinguser",
            "display_name": "Updated Name",
            "email": "updated@example.com",
            "avatar_url": "https://avatars.githubusercontent.com/u/11111",
        }

        user = find_or_create_user_by_github(db, github_data)
        assert user.github_id == 11111
        assert user.display_name == "Updated Name"
        # Email should NOT be overwritten if already set
        assert user.email == "old@example.com"
        db.commit.assert_called()
        db.add.assert_not_called()

    def test_find_or_create_google_new_user(self):
        """Should create a new user when Google ID not found."""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        google_data = {
            "google_id": "google_abc123",
            "display_name": "Google User",
            "email": "googleuser@example.com",
            "avatar_url": "https://lh3.googleusercontent.com/abc",
        }

        user = find_or_create_user_by_google(db, google_data)
        assert user is not None
        assert user.google_id == "google_abc123"
        assert user.display_name == "Google User"
        db.add.assert_called_once()
        db.commit.assert_called()

    def test_find_or_create_google_existing_user(self):
        """Should update and return existing user when Google ID found."""
        existing = User(
            id=uuid4(),
            google_id="google_xyz",
            display_name="Old G Name",
            email="oldg@example.com",
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = existing

        google_data = {
            "google_id": "google_xyz",
            "display_name": "Updated G Name",
            "email": "newg@example.com",
            "avatar_url": "https://lh3.googleusercontent.com/new",
        }

        user = find_or_create_user_by_google(db, google_data)
        assert user.google_id == "google_xyz"
        assert user.display_name == "Updated G Name"
        db.commit.assert_called()
        db.add.assert_not_called()


# ── Auth router endpoint tests ───────────────────────────────────────────


class TestAuthRoutes:
    """Test auth router endpoints using FastAPI TestClient.

    These tests instantiate the full FastAPI app via ``create_app()`` which
    constructs a real DB engine at import time. When Postgres is not
    available locally (CI / dev laptops without the docker stack up), the
    class-level skip kicks in instead of dumping connection-refused errors.
    """

    @pytest.fixture(autouse=True)
    def _require_postgres(self):
        """Skip the whole class when Postgres isn't reachable.

        secfix_1905/A added a root conftest.py that forces ``WR_DATABASE_URL``
        to sqlite so the production-secrets gate (Issue #1) doesn't fire
        during normal test runs. That flips the global engine to SQLite,
        which *is* reachable but doesn't match the integration-test assumptions
        here (Postgres-flavored DDL + the MCP session manager mounted by
        ``create_app()``). Skip when the engine isn't Postgres.
        """
        from app.database import engine
        from sqlalchemy.exc import OperationalError
        if engine.dialect.name != "postgresql":
            pytest.skip(f"Integration tests require Postgres; got {engine.dialect.name}")
        try:
            with engine.connect() as _c:
                _c.execute(__import__("sqlalchemy").text("SELECT 1"))
        except OperationalError as _e:
            pytest.skip(f"Postgres not reachable: {_e}")
        except Exception as _e:  # noqa: BLE001
            pytest.skip(f"DB probe failed: {type(_e).__name__}: {_e}")

    @pytest.fixture
    def client(self):
        """Create a test client with a fresh app using patched settings."""
        from fastapi.testclient import TestClient
        from app.main import create_app
        from app.config import settings as real_settings
        import app.auth_routes

        # Patch settings on the auth_routes module (used at request-time)
        original_settings = app.auth_routes.settings
        app.auth_routes.settings = real_settings

        with patch.object(real_settings, "GITHUB_CLIENT_ID", "test_gh_id"), \
             patch.object(real_settings, "GITHUB_CLIENT_SECRET", "test_gh_secret"), \
             patch.object(real_settings, "GOOGLE_CLIENT_ID", "test_google_id"), \
             patch.object(real_settings, "GOOGLE_CLIENT_SECRET", "test_google_secret"):

            app = create_app()
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c

    def test_github_login_redirects(self, client):
        """GET /api/auth/github/login should redirect to GitHub."""
        resp = client.get("/api/auth/github/login", follow_redirects=False)
        assert resp.status_code == 302
        assert "github.com/login/oauth/authorize" in resp.headers["location"]
        assert "client_id=test_gh_id" in resp.headers["location"]

    def test_github_login_sets_state_cookie(self, client):
        """GET /api/auth/github/login should set oauth_state cookie."""
        resp = client.get("/api/auth/github/login", follow_redirects=False)
        cookies = resp.cookies
        assert "oauth_state" in cookies

    def test_github_callback_no_code(self, client):
        """GET /api/auth/github/callback without code should redirect with error."""
        resp = client.get("/api/auth/github/callback", follow_redirects=False)
        assert resp.status_code == 302
        assert "auth=error" in resp.headers["location"]
        assert "no_code" in resp.headers["location"]

    def test_github_callback_no_state(self, client):
        """GET /api/auth/github/callback without state should redirect with error."""
        resp = client.get("/api/auth/github/callback?code=abc", follow_redirects=False)
        assert resp.status_code == 302
        assert "no_state" in resp.headers["location"]

    def test_google_login_redirects(self, client):
        """GET /api/auth/google/login should redirect to Google."""
        resp = client.get("/api/auth/google/login", follow_redirects=False)
        assert resp.status_code == 302
        assert "accounts.google.com/o/oauth2/v2/auth" in resp.headers["location"]
        assert "client_id=test_google_id" in resp.headers["location"]

    def test_google_callback_no_code(self, client):
        """GET /api/auth/google/callback without code should redirect with error."""
        resp = client.get("/api/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 302
        assert "no_code" in resp.headers["location"]

    def test_me_without_auth(self, client):
        """GET /api/auth/me without token should return 401."""
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_me_with_valid_bearer(self, client):
        """GET /api/auth/me with valid Bearer token should return user."""
        from app.database import SessionLocal
        from app.models import User as UserModel

        db = SessionLocal()
        try:
            user_id = uuid4()
            user = UserModel(
                id=user_id,
                display_name="Bearer Test User",
                email="bearer@example.com",
            )
            db.add(user)
            db.commit()

            token = create_jwt(user)
            resp = client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["display_name"] == "Bearer Test User"
            assert data["email"] == "bearer@example.com"
        finally:
            # Cleanup test user
            db.query(UserModel).filter(UserModel.id == user_id).delete()
            db.commit()
            db.close()

    def test_me_with_invalid_bearer(self, client):
        """GET /api/auth/me with invalid token should return 401."""
        resp = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer invalid.token.here"},
        )
        assert resp.status_code == 401
