"""Phase N — CORS middleware regression tests.

Verifies that:
  1. An allowed origin (recipes.wisechef.ai) receives the correct
     ``Access-Control-Allow-Origin`` response header.
  2. A disallowed origin (evil.com) does NOT receive CORS headers
     (Starlette CORSMiddleware simply omits the header when the origin is
     not in the allow-list).
  3. OPTIONS preflight from an allowed origin is answered with 200 and the
     correct CORS headers.
  4. OPTIONS preflight from a disallowed origin is answered without CORS
     headers.

The tests build a *minimal* FastAPI app that mirrors the CORS configuration
in ``app/main.py`` — this avoids pulling in the full application stack
(DB, Stripe, Discord, etc.) and keeps the suite fast and self-contained.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

# ── Allowed origins (must match app/main.py) ─────────────────────────────────
ALLOWED_ORIGINS = [
    "https://recipes.wisechef.ai",
    "https://www.recipes.wisechef.ai",
]

DISALLOWED_ORIGIN = "https://evil.com"


def _make_app() -> FastAPI:
    """Minimal FastAPI app wired with the production CORS policy."""
    app = FastAPI()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["x-api-key", "authorization", "content-type"],
    )

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


_app = _make_app()
_client = TestClient(_app, raise_server_exceptions=True)

# ── Helpers ───────────────────────────────────────────────────────────────────

ACAO_HEADER = "access-control-allow-origin"


# ── Tests: simple (non-preflight) requests ────────────────────────────────────


def test_allowed_origin_receives_cors_header():
    """A GET from an allowed origin gets Access-Control-Allow-Origin echoed back."""
    resp = _client.get(
        "/healthz",
        headers={"Origin": "https://recipes.wisechef.ai"},
    )
    assert resp.status_code == 200
    assert ACAO_HEADER in resp.headers, "Expected CORS header for allowed origin"
    assert resp.headers[ACAO_HEADER] == "https://recipes.wisechef.ai"


def test_www_allowed_origin_receives_cors_header():
    """The www-prefixed variant is also in the allow-list."""
    resp = _client.get(
        "/healthz",
        headers={"Origin": "https://www.recipes.wisechef.ai"},
    )
    assert resp.status_code == 200
    assert ACAO_HEADER in resp.headers, "Expected CORS header for www variant"
    assert resp.headers[ACAO_HEADER] == "https://www.recipes.wisechef.ai"


def test_disallowed_origin_gets_no_cors_header():
    """A GET from an untrusted origin must NOT receive Access-Control-Allow-Origin."""
    resp = _client.get(
        "/healthz",
        headers={"Origin": DISALLOWED_ORIGIN},
    )
    # The route still responds (server-side CORS is advisory for browsers, but
    # the header must be absent so the browser blocks the response).
    assert resp.status_code == 200
    assert ACAO_HEADER not in resp.headers, (
        f"CORS header must be absent for disallowed origin, got: "
        f"{resp.headers.get(ACAO_HEADER)}"
    )


# ── Tests: OPTIONS preflight ──────────────────────────────────────────────────


def test_preflight_allowed_origin():
    """OPTIONS preflight from an allowed origin is answered with 200 and CORS headers."""
    resp = _client.options(
        "/healthz",
        headers={
            "Origin": "https://recipes.wisechef.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-api-key, content-type",
        },
    )
    assert resp.status_code == 200
    assert ACAO_HEADER in resp.headers, "Expected CORS header in preflight response"
    assert resp.headers[ACAO_HEADER] == "https://recipes.wisechef.ai"


def test_preflight_disallowed_origin():
    """OPTIONS preflight from a disallowed origin must NOT receive CORS headers."""
    resp = _client.options(
        "/healthz",
        headers={
            "Origin": DISALLOWED_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert ACAO_HEADER not in resp.headers, (
        "CORS header must be absent in preflight for disallowed origin"
    )


# ── Tests: no Origin header (non-browser clients like MCP agents) ─────────────


def test_no_origin_header_returns_no_cors_header():
    """Requests without an Origin header (e.g. MCP agents, CLI) get no CORS headers.

    This confirms that the restrictive allow-list does not interfere with
    programmatic API consumers that never send Origin.
    """
    resp = _client.get("/healthz")
    assert resp.status_code == 200
    assert ACAO_HEADER not in resp.headers, (
        "No CORS headers expected when Origin is absent"
    )
