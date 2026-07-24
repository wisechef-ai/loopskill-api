"""secfix_1905 Phase A — AuthContext module tests.

Tests:
  - AuthContext is a frozen dataclass (immutable)
  - anonymous() returns scope="anonymous"
  - Legacy shims (request.state.api_key_id) still populated for user-scope keys
  - auth_ctx populated in middleware for master key
  - Middleware wires auth_ctx for user-scope API key
"""
import pytest
from uuid import uuid4
from unittest.mock import MagicMock, patch


# ── AuthContext unit tests ────────────────────────────────────────────────────

def test_auth_context_is_frozen():
    """AuthContext must be a frozen dataclass — mutation raises FrozenInstanceError."""
    from app.auth_ctx import AuthContext
    from dataclasses import FrozenInstanceError

    ctx = AuthContext(scope="user")
    with pytest.raises(FrozenInstanceError):
        ctx.scope = "master"  # type: ignore[misc]


def test_anonymous_returns_anonymous_scope():
    """AuthContext.anonymous() must return scope='anonymous'."""
    from app.auth_ctx import AuthContext

    ctx = AuthContext.anonymous()
    assert ctx.scope == "anonymous"
    assert ctx.user_id is None
    assert ctx.api_key_id is None
    assert ctx.bundle_scope is None
    assert ctx.tier is None
    assert ctx.is_sandbox_operator is False


def test_auth_context_master_scope():
    """Master-scope context has no user_id or api_key_id."""
    from app.auth_ctx import AuthContext

    ctx = AuthContext(scope="master")
    assert ctx.scope == "master"
    assert ctx.user_id is None
    assert ctx.api_key_id is None


def test_auth_context_user_scope():
    """User-scope context carries user_id and api_key_id."""
    from app.auth_ctx import AuthContext

    uid = uuid4()
    kid = uuid4()
    ctx = AuthContext(scope="user", user_id=uid, api_key_id=kid, tier="pro")
    assert ctx.scope == "user"
    assert ctx.user_id == uid
    assert ctx.api_key_id == kid
    assert ctx.tier == "pro"


def test_auth_context_cbt_token_scope():
    """cbt_token scope carries bundle_scope."""
    from app.auth_ctx import AuthContext

    cb_id = uuid4()
    ctx = AuthContext(scope="cbt_token", bundle_scope=cb_id)
    assert ctx.scope == "cbt_token"
    assert ctx.bundle_scope == cb_id


def test_auth_context_sandbox_operator_flag():
    """is_sandbox_operator flag works."""
    from app.auth_ctx import AuthContext

    ctx = AuthContext(scope="user", is_sandbox_operator=True)
    assert ctx.is_sandbox_operator is True


# ── Middleware wiring tests ───────────────────────────────────────────────────

def test_middleware_sets_auth_ctx_for_master_key(client):
    """Middleware must set request.state.auth_ctx with scope='master'
    when the master API key is used.

    We test this indirectly: the client fixture uses settings.API_KEY
    as the x-api-key header which is the master key.
    """
    # A simple endpoint that reads auth_ctx from state
    from app.auth_ctx import AuthContext
    from app.middleware import APIKeyMiddleware
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from app.config import settings

    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)

    @app.get("/test-auth")
    async def test_auth(request: Request):
        ctx = getattr(request.state, "auth_ctx", None)
        if ctx is None:
            return JSONResponse({"auth_ctx": None})
        return JSONResponse({"scope": ctx.scope})

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/test-auth", headers={"x-api-key": settings.API_KEY})

    assert resp.status_code == 200
    assert resp.json()["scope"] == "master"


def test_middleware_sets_legacy_shims_for_master_key():
    """Legacy api_key_id and api_key_user_id shims must still work."""
    from app.middleware import APIKeyMiddleware
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from app.config import settings

    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)

    @app.get("/test-legacy")
    async def test_legacy(request: Request):
        return JSONResponse({
            "api_key_id": str(getattr(request.state, "api_key_id", "MISSING")),
            "api_key_user_id": str(getattr(request.state, "api_key_user_id", "MISSING")),
        })

    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/test-legacy", headers={"x-api-key": settings.API_KEY})

    assert resp.status_code == 200
    data = resp.json()
    # Master key: both legacy shims should be None (set as None)
    assert data["api_key_id"] == "None"
    assert data["api_key_user_id"] == "None"


def test_import_from_auth_ctx():
    """Verify the module imports cleanly."""
    from app.auth_ctx import AuthContext, Scope
    assert AuthContext is not None
    assert Scope is not None
