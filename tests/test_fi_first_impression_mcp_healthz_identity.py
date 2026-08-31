"""fi_first_impression_api — /api/mcp/healthz identity rename + real version.

THE GAP
-------
Live probe (2026-08-19): GET /api/mcp/healthz returned
``{"name": "recipes-mcp", "version": "0.1.0", ...}`` — a dead brand name
(the repo was renamed to loopskill-api long ago, per AGENTS.md's header)
and a version literal that had never once been bumped since this file's
introduction, so the machine-readable MCP identity could never prove which
deploy was actually live. This is the same "identity surface must read the
single-sourced app version" contract test_activate0701_version_contract.py
already enforces for health_routes.py/main.py — server.py was carved out as
a "distinct version concept" (the MCP protocol handshake value, which is
legitimately allowed to differ from the app version) but nothing was
actually WIRING it to move together; it just sat frozen at "0.1.0".

FIX
---
SERVER_NAME -> "loopskill-mcp". SERVER_VERSION now reads
``app.version.__version__`` directly (imported at module top, referenced as
``_APP_VERSION``) instead of a hardcoded string — so it moves in lockstep
with every deploy's real version, with zero new literal to drift.
"""

from __future__ import annotations

from app.mcp.server import SERVER_NAME, SERVER_VERSION
from app.version import __version__


def test_server_name_is_the_loopskill_brand_not_the_dead_recipes_brand():
    assert SERVER_NAME == "loopskill-mcp"
    assert "recipes" not in SERVER_NAME


def test_server_version_reads_the_single_sourced_app_version():
    """SERVER_VERSION must track app.version.__version__ exactly — not a
    frozen literal that only happens to look like a version string."""
    assert SERVER_VERSION == __version__


def test_healthz_reports_the_new_identity(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.database import get_db
    from app.mcp.server import router as mcp_router

    app = FastAPI()

    def override_get_db():
        try:
            yield None
        finally:
            pass

    app.include_router(mcp_router)
    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, raise_server_exceptions=True) as c:
        resp = c.get("/api/mcp/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "loopskill-mcp"
    assert body["version"] == __version__
    assert body["version"] != "0.1.0"
