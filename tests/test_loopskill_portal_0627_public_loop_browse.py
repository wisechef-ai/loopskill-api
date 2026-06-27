"""loopskill_portal_0627 — public loop BROWSE, protected loop WRITES.

The runnable loop registry is the #1 stars-conversion surface, so the portal
hero must render the loop library (browse + detail) for anonymous visitors
without a baked-in API key. This adds ``/api/loops`` to the middleware's
PUBLIC_PREFIXES.

PUBLIC_PREFIXES is METHOD-AGNOSTIC — exempting the prefix lets every method
through the middleware. The safety contract that makes this acceptable is that
the three write routes (run / rate / publish) each SELF-ENFORCE auth by reading
``request.state.auth_ctx`` and raising 401 for anonymous scope. This test pins
BOTH halves so a future refactor can't quietly:
  (a) drop ``/api/loops`` from the allowlist (breaking the public hero), or
  (b) remove a route's self-guard (turning the method-agnostic prefix into an
      anonymous-write hole).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth_ctx import AuthContext
from app.database import get_db
from app.loop_routes import router as loop_router
from app.middleware.api_key import APIKeyMiddleware


# ── 1. allowlist classification (the public-browse half) ─────────────────────


def test_loops_prefix_is_in_public_allowlist():
    """GET /api/loops (browse) + /api/loops/{slug} (detail) must be public so
    the portal hero renders without an API key."""
    assert "/api/loops" in set(APIKeyMiddleware.PUBLIC_PREFIXES), (
        "/api/loops dropped from PUBLIC_PREFIXES — the public loop-library hero "
        "will 401 for anonymous visitors."
    )


def test_loops_prefix_covers_browse_and_detail():
    """The prefix is a startswith() match, so it must cover both the bare list
    path and the slug-detail path."""
    prefixes = tuple(APIKeyMiddleware.PUBLIC_PREFIXES)
    for path in ("/api/loops", "/api/loops/hello-world-loop"):
        assert any(path.startswith(p) for p in prefixes), (
            f"{path} is not matched by any public prefix"
        )


# ── 2. route self-enforcement (the protected-writes half) ────────────────────


@pytest.fixture()
def loops_client(db_session):
    """Real loop router behind a stub that mirrors APIKeyMiddleware's anonymous
    stamping for a public prefix: NO key => AuthContext.anonymous() is stamped
    (NOT a 401 from the middleware). The route's own guard must do the rejecting.
    """
    app = FastAPI()

    @app.middleware("http")
    async def _stub_public_prefix_auth(request: Request, call_next):
        hdr = request.headers.get("x-test-auth")
        if hdr == "user":
            request.state.auth_ctx = AuthContext(scope="user", user_id=uuid4())
        elif hdr == "master":
            request.state.auth_ctx = AuthContext(scope="master")
        else:
            # Mirrors the PUBLIC_PREFIXES branch: anonymous ctx is stamped and
            # the request is allowed THROUGH the middleware.
            request.state.auth_ctx = AuthContext.anonymous()
        return await call_next(request)

    app.include_router(loop_router)

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, raise_server_exceptions=True)


def test_anonymous_run_is_rejected_even_though_prefix_is_public(loops_client):
    """POST /run must 401 for anonymous — the route self-guards; the public
    prefix does NOT make writes anonymous-accessible."""
    res = loops_client.post("/api/loops/whatever/run", json={})
    assert res.status_code == 401, (
        f"anonymous /run should 401, got {res.status_code} — the method-agnostic "
        "public prefix has become an anonymous-write hole."
    )


def test_anonymous_rate_is_rejected(loops_client):
    res = loops_client.post("/api/loops/whatever/rate", json={"rating": 5})
    assert res.status_code == 401, (
        f"anonymous /rate should 401, got {res.status_code}"
    )


def test_anonymous_publish_is_rejected(loops_client):
    # A SCHEMA-VALID body so FastAPI's 422 body-validation can't pre-empt the
    # route's auth guard — we are pinning the AUTH gate, not request validation.
    res = loops_client.post(
        "/api/loops",
        json={
            "slug": "x",
            "title": "x",
            "description": "x",
            "success_condition": "x",
            "verification_script": "true",
            "system_prompt": "x",
            "stopping_criteria": {},
        },
    )
    assert res.status_code == 401, (
        f"anonymous publish should 401, got {res.status_code}"
    )
