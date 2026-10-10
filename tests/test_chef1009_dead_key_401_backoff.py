"""chef_2026-10-09-E — dead-key 401 backoff hint on /api/mcp/http/.

Baseline (t_ffdc11c0, 2026-09-24): ~88% of 24h 4xx/5xx on loopskill-api were
dead-credential MCP clients retrying /api/mcp/http/ indefinitely (~75s
cadence) because the bare 401 carried no backoff signal. Every 401 emitted by
the StreamableHTTP mount's auth gate must now carry ``Retry-After`` so
RFC-compliant clients back off.

Pinned here:
  1. missing/empty key  -> 401 + Retry-After (fast-path branch)
  2. malformed key      -> 401 + Retry-After (fast-path branch)
  3. unknown rec_ key   -> 401 + Retry-After (DB-lookup branch)
  4. success path       -> 200 and NO Retry-After (hint must never leak
                           onto authenticated traffic)
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db

RETRY_AFTER = "3600"


@pytest.fixture()
def mcp_http_app(db_session):
    """Minimal app with ONLY the /api/mcp/http mount + DB override.

    Mirrors tests/test_mcp_streamable_transport.py::mcp_app but without the
    SSE router, so a stray route cannot answer before the auth gate.
    """
    from contextlib import asynccontextmanager

    from app.mcp.server import (
        _build_streamable_http_mount,
        _reset_http_session_manager,
    )

    _reset_http_session_manager()

    app = FastAPI()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.router.routes.append(_build_streamable_http_mount())

    @asynccontextmanager
    async def _lifespan(app):
        from app.mcp.server import run_streamable_http

        async with run_streamable_http():
            yield

    app.router.lifespan_context = _lifespan
    return app


@pytest.fixture()
def mcp_http_client(mcp_http_app):
    with TestClient(mcp_http_app, raise_server_exceptions=True) as c:
        yield c


class TestDeadKey401BackoffHint:
    """Every 401 from the StreamableHTTP mount carries Retry-After."""

    def test_missing_key_401_carries_retry_after(self, mcp_http_client):
        resp = mcp_http_client.post("/api/mcp/http", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert resp.status_code == 401
        assert resp.headers.get("retry-after") == RETRY_AFTER

    def test_malformed_key_401_carries_retry_after(self, mcp_http_client):
        resp = mcp_http_client.post(
            "/api/mcp/http",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={"x-api-key": "not-a-loopskill-key"},
        )
        assert resp.status_code == 401
        assert resp.headers.get("retry-after") == RETRY_AFTER

    def test_unknown_rec_key_401_carries_retry_after(self, mcp_http_client, monkeypatch, db_session):
        """The DB-lookup branch (format-valid key, 0 rows in api_keys) — the
        exact failure mode of the baseline's top offender."""
        from app.mcp import server as server_mod

        class _NonClosingSession:
            def __init__(self, sess):
                self._sess = sess

            def __getattr__(self, name):
                return getattr(self._sess, name)

            def close(self):
                pass

        monkeypatch.setattr(
            "app.database.SessionLocal",
            lambda: _NonClosingSession(db_session),
        )

        # Prove the gate short-circuits BEFORE the session manager.
        mgr = server_mod.get_http_session_manager()

        async def fail_handle_request(scope, receive, send):
            raise AssertionError("unauthorized request must not reach the session manager")

        monkeypatch.setattr(mgr, "handle_request", fail_handle_request)

        resp = mcp_http_client.post(
            "/api/mcp/http",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            headers={
                "x-api-key": "rec_live_doesnotexist000000",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert resp.status_code == 401
        assert resp.headers.get("retry-after") == RETRY_AFTER

    def test_authenticated_request_has_no_retry_after(self, mcp_http_client):
        """The hint must never leak onto authenticated traffic (initialize
        with the master key succeeds and carries no Retry-After header)."""
        resp = mcp_http_client.post(
            "/api/mcp/http",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "backoff-test", "version": "1"},
                },
            },
            headers={
                "x-api-key": settings.API_KEY,
                "Accept": "application/json, text/event-stream",
            },
        )
        assert resp.status_code == 200, f"initialize failed: {resp.text}"
        assert "retry-after" not in {k.lower() for k in resp.headers}
