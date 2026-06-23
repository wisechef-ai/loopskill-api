"""RCP-7 — RateLimitMiddleware regression tests.

Locks the OAuth-429 fix from 2026-05-08:
  1. Behind Cloudflare, the real client IP MUST be taken from
     ``CF-Connecting-IP`` — not ``request.client.host``. Otherwise every
     visitor shares the edge IP's bucket and login gets locked instantly.
  2. ``/api/auth/{github,google}/*`` MUST be exempt — these are one-shot
     OAuth redirects that the upstream provider already rate-limits.
"""
from __future__ import annotations

import time
from collections import defaultdict
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware import RateLimitMiddleware


@pytest.fixture
def small_app():
    """FastAPI app with a tight 3-req/sec limit so we can exhaust it fast."""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, max_requests=3, window_seconds=60)

    @app.get("/api/anything")
    async def anything():
        return {"ok": True}

    @app.get("/api/auth/github/login")
    async def github_login():
        return {"redirect": "github"}

    @app.get("/api/auth/google/login")
    async def google_login():
        return {"redirect": "google"}

    @app.get("/api/auth/google/callback")
    async def google_callback():
        return {"ok": True}

    return app


# Force in-memory limiter so tests don't depend on Redis.
@pytest.fixture(autouse=True)
def _no_redis():
    with patch("app.middleware.get_redis", return_value=None):
        yield


def test_rate_limit_uses_cf_connecting_ip(small_app):
    """Two distinct CF-Connecting-IP values get distinct buckets.

    Issue #12 update: CF-Connecting-IP is only honoured when the TCP peer is
    in TRUSTED_PROXY_CIDRS. We patch the setting to include 0.0.0.0/0 so
    the TestClient's 'testclient' host is treated as a trusted proxy.
    """
    from unittest.mock import patch
    client = TestClient(small_app)

    with patch("app.middleware.settings") as mock_settings, \
         patch("app.utils.client_ip._is_trusted", return_value=True):
        # Visitor A burns through the limit.
        for _ in range(3):
            r = client.get("/api/anything", headers={"cf-connecting-ip": "1.1.1.1"})
            assert r.status_code == 200
        r = client.get("/api/anything", headers={"cf-connecting-ip": "1.1.1.1"})
        assert r.status_code == 429, "visitor A should be limited after 3 hits"

        # Visitor B is independent.
        r = client.get("/api/anything", headers={"cf-connecting-ip": "2.2.2.2"})
        assert r.status_code == 200, "visitor B must not share visitor A's bucket"


def test_rate_limit_falls_back_to_xff(small_app):
    """When no CF header, X-Forwarded-For first hop is used from trusted proxy.

    Issue #12 update: XFF is only honoured from trusted proxies. We patch
    _is_trusted to return True so the TestClient is treated as trusted.
    """
    client = TestClient(small_app)

    with patch("app.utils.client_ip._is_trusted", return_value=True):
        for _ in range(3):
            r = client.get(
                "/api/anything",
                headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1, 192.168.0.1"},
            )
            assert r.status_code == 200
        r = client.get(
            "/api/anything",
            headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1, 192.168.0.1"},
        )
        assert r.status_code == 429

        # Different first-hop = different bucket.
        r = client.get("/api/anything", headers={"x-forwarded-for": "8.8.8.8"})
        assert r.status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/github/login",
        "/api/auth/google/login",
        "/api/auth/google/callback",
    ],
)
def test_oauth_paths_are_exempt(small_app, path):
    """OAuth login/callback endpoints must never 429 — they break sign-in."""
    client = TestClient(small_app)
    # 50 hits, single IP, all exempt — none should 429.
    for _ in range(50):
        r = client.get(path, headers={"cf-connecting-ip": "1.1.1.1"})
        assert r.status_code == 200, (
            f"OAuth path {path} got rate-limited (status={r.status_code}); "
            "this would lock every visitor out of sign-in."
        )


def test_oauth_exemption_is_prefix_not_substring(small_app):
    """Make sure something like /not/api/auth/github/foo isn't accidentally exempted."""

    @small_app.get("/decoy/api/auth/github/login")
    async def decoy():
        return {"ok": True}

    client = TestClient(small_app)
    for _ in range(3):
        client.get("/decoy/api/auth/github/login", headers={"cf-connecting-ip": "1.1.1.1"})
    r = client.get("/decoy/api/auth/github/login", headers={"cf-connecting-ip": "1.1.1.1"})
    assert r.status_code == 429, "non-prefix matches must still be rate-limited"


# ---------------------------------------------------------------------------
# Phase-G+ — authenticated-bypass regression
# ---------------------------------------------------------------------------
#
# Symptom that motivated this: the Astro portal build fires ~116 page-render
# fetches against the API from a single egress IP in a few hundred ms. With
# the 60-req/min per-IP cap that bucket bursts instantly, the hero stats
# fetch hits API 429 even with retry-and-jitter, and the page falls back to
# the hardcoded "59+" — which is stale by 5 skills.
#
# Fix: any caller that already passed APIKeyMiddleware with a real scope
# (master, user, fleet, cookbook, …) bypasses the per-IP bucket. Anonymous
# traffic is still bucketed. Per-key abuse is bounded elsewhere (install
# routes have TIER_INSTALL_LIMITS, MCP tools have their own quotas).


@pytest.fixture
def authed_app():
    """Like ``small_app`` but seeds request.state.auth_ctx for authenticated calls.

    The real production stack populates ``auth_ctx`` in APIKeyMiddleware
    BEFORE RateLimitMiddleware sees the request. We simulate that here with
    a tiny BaseHTTPMiddleware that reads ``x-test-scope`` (set by the test)
    and attaches a stub. It MUST be added AFTER RateLimitMiddleware so it
    wraps it (Starlette LIFO — last added is outermost on the request path,
    so it runs FIRST and populates auth_ctx before RateLimit sees the call).
    """
    from types import SimpleNamespace

    from starlette.middleware.base import BaseHTTPMiddleware

    class _AuthInjector(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            scope = request.headers.get("x-test-scope")
            if scope:
                request.state.auth_ctx = SimpleNamespace(scope=scope)
            return await call_next(request)

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, max_requests=3, window_seconds=60)
    app.add_middleware(_AuthInjector)  # added last → outermost → runs first

    @app.get("/api/anything")
    async def anything():
        return {"ok": True}

    return app


@pytest.mark.parametrize(
    "scope",
    ["master", "user", "fleet", "cookbook"],
)
def test_authenticated_callers_bypass_per_ip_bucket(authed_app, scope):
    """A real authenticated scope MUST get past the 3-req/min cap.

    Regression target: the portal build hit 429 on /api/skills/search?page_size=1
    after ~3-5 hero fetches, falling back to a hardcoded count. After this
    fix, authenticated callers pass through unrestricted by IP.
    """
    from unittest.mock import patch
    client = TestClient(authed_app)

    with patch("app.utils.client_ip._is_trusted", return_value=True):
        # 50 hits, single IP, single key — every one MUST return 200.
        for i in range(50):
            r = client.get(
                "/api/anything",
                headers={
                    "cf-connecting-ip": "1.1.1.1",
                    "x-test-scope": scope,
                },
            )
            assert r.status_code == 200, (
                f"authenticated {scope} call #{i+1} was rate-limited "
                f"(status={r.status_code}); this would break build-time "
                "portal fetches and MCP fleet sync."
            )


def test_anonymous_callers_still_bucketed(authed_app):
    """No auth header → falls into the per-IP bucket as before."""
    from unittest.mock import patch
    client = TestClient(authed_app)

    with patch("app.utils.client_ip._is_trusted", return_value=True):
        for _ in range(3):
            r = client.get("/api/anything", headers={"cf-connecting-ip": "1.1.1.1"})
            assert r.status_code == 200
        r = client.get("/api/anything", headers={"cf-connecting-ip": "1.1.1.1"})
        assert r.status_code == 429, (
            "anonymous traffic must STILL be bucketed — the bypass only "
            "applies to authenticated callers."
        )


def test_anonymous_scope_literal_still_bucketed(authed_app):
    """``scope='anonymous'`` is treated the same as no auth_ctx at all.

    Defensive: if a future code path sets auth_ctx with scope='anonymous'
    (rather than leaving it None), it must still hit the bucket.
    """
    from unittest.mock import patch
    client = TestClient(authed_app)

    with patch("app.utils.client_ip._is_trusted", return_value=True):
        for _ in range(3):
            r = client.get(
                "/api/anything",
                headers={"cf-connecting-ip": "1.1.1.1", "x-test-scope": "anonymous"},
            )
            assert r.status_code == 200
        r = client.get(
            "/api/anything",
            headers={"cf-connecting-ip": "1.1.1.1", "x-test-scope": "anonymous"},
        )
        assert r.status_code == 429
