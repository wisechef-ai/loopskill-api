"""loopskill_portal_0627 — public personality BROWSE, protected WRITES.

Mirrors test_loopskill_portal_0627_public_loop_browse.py for the personalities
registry. The portal /personalities page must render the catalog for anonymous
visitors (GET browse + detail public), while publish stays auth-gated.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth_ctx import AuthContext
from app.database import get_db
from app.middleware.api_key import APIKeyMiddleware
from app.personality_routes import router as personality_router


def test_personalities_prefix_is_public():
    assert "/api/personalities" in set(APIKeyMiddleware.PUBLIC_PREFIXES), (
        "/api/personalities dropped from PUBLIC_PREFIXES — the portal page will 401."
    )


def test_personalities_prefix_covers_browse_and_detail():
    prefixes = tuple(APIKeyMiddleware.PUBLIC_PREFIXES)
    for path in ("/api/personalities", "/api/personalities/some-slug"):
        assert any(path.startswith(p) for p in prefixes), f"{path} not public"


@pytest.fixture()
def pers_client(db_session):
    app = FastAPI()

    @app.middleware("http")
    async def _stub(request: Request, call_next):
        hdr = request.headers.get("x-test-auth")
        if hdr == "user":
            request.state.auth_ctx = AuthContext(scope="user", user_id=uuid4())
        elif hdr == "master":
            request.state.auth_ctx = AuthContext(scope="master")
        else:
            request.state.auth_ctx = AuthContext.anonymous()
        return await call_next(request)

    app.include_router(personality_router)

    def _db():
        yield db_session

    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=True)


def test_anonymous_publish_personality_rejected(pers_client):
    # schema-valid body so 422 can't pre-empt the auth guard
    res = pers_client.post(
        "/api/personalities",
        json={"slug": "x", "title": "x", "system_prompt": "be helpful"},
    )
    assert res.status_code == 401, (
        f"anonymous personality publish should 401, got {res.status_code}"
    )
