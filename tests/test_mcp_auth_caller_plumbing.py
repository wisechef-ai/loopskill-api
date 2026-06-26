"""RCP-10 — MCP server must plumb authenticated caller into every tool call.

Before this fix, ``build_mcp_server()._call_tool`` hardcoded
``caller={"scope": "operator", "user_id": None}`` and dropped the auth
result resolved by ``_authenticate``. As a consequence:

* ``recipes_list_cookbook`` called without an explicit ``cookbook_id``
  always fell through to the no-cookbook branch (since ``user_id=None``
  matches no row).
* ``recipes_install`` recorded ``InstallEvent.api_key_id=NULL`` even when
  the caller authenticated with a real APIKey — defeating per-key
  install analytics.
* ``recipes_sync`` saw an empty operator caller for every request.

This test module verifies the per-call plumbing that closes the gap:
``_authenticate`` (SSE) and the StreamableHTTP ASGI auth wrapper both
stash the resolved caller dict on ``scope["state"]["mcp_caller"]``;
``_caller_from_request_context`` retrieves it inside ``_call_tool``;
the stdio / direct-dispatch path falls back to the operator default.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import mcp.types as mcp_types
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import settings
from app.database import get_db
from app.mcp.auth import validate_key
from app.mcp.server import (
    _caller_from_request_context,
    _tool_definitions,
    build_mcp_server,
    router as mcp_router,
)
from app.models import APIKey, Bundle, BundleSkill, InstallEvent, SkillVersion, User
from tests.conftest import make_skill


# ── Helpers ────────────────────────────────────────────────────────────────


def _make_user_and_key(db, key_value: str = "rec_user_secret_key") -> tuple[User, APIKey]:
    """Create a User + APIKey pair and return them. Commits to db."""
    user = User(id=uuid4(), display_name="Test User")
    db.add(user)
    db.flush()
    api_key = APIKey(
        id=uuid4(),
        user_id=user.id,
        key_prefix=key_value[:8],
        key_hash=hashlib.sha256(key_value.encode()).hexdigest(),
        name="test",
        is_active=True,
    )
    db.add(api_key)
    db.flush()
    return user, api_key


def _drive_call_tool(server, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Drive the MCP Server's call_tool handler in-process.

    Returns the parsed JSON payload. NOTE: this path does *not* go through
    the SSE/HTTP transports' RequestContext, so the dispatch sees an
    empty request_context — i.e. the stdio/operator-fallback codepath.
    """
    handler = server.request_handlers[mcp_types.CallToolRequest]
    req = mcp_types.CallToolRequest(
        method="tools/call",
        params=mcp_types.CallToolRequestParams(name=name, arguments=arguments),
    )
    result = asyncio.run(handler(req))
    payload_text = result.root.content[0].text  # type: ignore[attr-defined]
    return json.loads(payload_text)


def _drive_call_tool_with_caller(
    server,
    name: str,
    arguments: dict[str, Any],
    caller: dict[str, Any],
) -> dict[str, Any]:
    """Drive call_tool with a synthetic RequestContext that carries
    ``request.state.mcp_caller`` — i.e. what the SSE/HTTP transports
    actually present at runtime.
    """
    from mcp.shared.context import RequestContext
    from mcp.server.lowlevel.server import request_ctx

    # Build a Starlette Request with state pre-populated. Two Request
    # instances over the same scope share request.state via scope["state"],
    # which is exactly how the production transports plumb auth across
    # the auth-gate -> dispatch boundary.
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [],
        "path": "/api/mcp/messages/",
        "query_string": b"",
    }

    async def _recv():  # pragma: no cover - never invoked
        return {"type": "http.request", "body": b""}

    request = Request(scope, _recv)
    request.state.mcp_caller = caller

    fake_ctx = RequestContext(
        request_id="test-req",
        meta=None,
        session=None,  # type: ignore[arg-type]
        lifespan_context=None,
        request=request,
    )
    token = request_ctx.set(fake_ctx)
    try:
        return _drive_call_tool(server, name, arguments)
    finally:
        request_ctx.reset(token)


# ── _caller_from_request_context unit ──────────────────────────────────────


class TestCallerResolution:
    """Direct unit coverage for the request-context unwrapper."""

    def test_returns_fallback_outside_request_context(self):
        server = build_mcp_server()
        caller = _caller_from_request_context(server)
        assert caller == {"scope": "operator", "user_id": None, "api_key_id": None}

    def test_returns_stashed_caller_inside_request_context(self):
        from mcp.shared.context import RequestContext
        from mcp.server.lowlevel.server import request_ctx

        scope = {"type": "http", "headers": [], "path": "/", "query_string": b""}

        async def _recv():
            return {}

        request = Request(scope, _recv)
        stashed = {
            "scope": "operator",
            "user_id": uuid4(),
            "api_key_id": uuid4(),
        }
        request.state.mcp_caller = stashed

        server = build_mcp_server()
        ctx = RequestContext(
            request_id="r",
            meta=None,
            session=None,  # type: ignore[arg-type]
            lifespan_context=None,
            request=request,
        )
        token = request_ctx.set(ctx)
        try:
            assert _caller_from_request_context(server) == stashed
        finally:
            request_ctx.reset(token)

    def test_returns_fallback_when_state_has_no_caller(self):
        """If our auth layer didn't run (shouldn't happen in production but
        guards against regressions), we fall back cleanly instead of crashing.
        """
        from mcp.shared.context import RequestContext
        from mcp.server.lowlevel.server import request_ctx

        scope = {"type": "http", "headers": [], "path": "/", "query_string": b""}

        async def _recv():
            return {}

        request = Request(scope, _recv)
        # Note: no request.state.mcp_caller set.

        server = build_mcp_server()
        ctx = RequestContext(
            request_id="r",
            meta=None,
            session=None,  # type: ignore[arg-type]
            lifespan_context=None,
            request=request,
        )
        token = request_ctx.set(ctx)
        try:
            caller = _caller_from_request_context(server)
            assert caller == {"scope": "operator", "user_id": None, "api_key_id": None}
        finally:
            request_ctx.reset(token)


# ── validate_key contract ──────────────────────────────────────────────────


class TestValidateKeyApiKeyId:
    """``validate_key`` must return ``api_key_id`` so InstallEvent rows can
    record per-key install analytics."""

    def test_master_key_returns_null_api_key_id(self, db_session):
        result = validate_key(settings.API_KEY, db_session)
        # Phase B (Issue #5): master key now returns scope='master' not 'operator'
        assert result["scope"] == "master"
        assert result["user_id"] is None
        assert result["api_key_id"] is None
        # auth_ctx should be present and correct
        from app.auth_ctx import AuthContext
        assert isinstance(result.get("auth_ctx"), AuthContext)
        assert result["auth_ctx"].scope == "master"

    def test_unauthorized_returns_null_api_key_id(self, db_session):
        result = validate_key("not_a_real_key", db_session)
        assert result["scope"] == "unauthorized"
        assert result["api_key_id"] is None
        assert result["user_id"] is None

    def test_user_scoped_key_returns_user_id_and_api_key_id(self, db_session):
        user, api_key = _make_user_and_key(db_session, "rec_alice_token")
        result = validate_key("rec_alice_token", db_session)
        # Phase B (Issue #5): user key now returns scope='user' not 'operator'
        assert result["scope"] == "user"
        assert result["user_id"] == user.id
        assert result["api_key_id"] == api_key.id

    def test_inactive_key_is_unauthorized(self, db_session):
        user, api_key = _make_user_and_key(db_session, "rec_disabled_token")
        api_key.is_active = False
        db_session.flush()
        result = validate_key("rec_disabled_token", db_session)
        assert result["scope"] == "unauthorized"
        assert result["api_key_id"] is None


# ── recipes_list_cookbook — the original RCP-10 bug ───────────────────────


class TestListCookbookUsesCallerUserId:
    """Original bug: ``list_cookbook`` called without ``cookbook_id`` always
    fell through to the no-cookbook branch because the dispatch hardcoded
    ``user_id=None``. With the fix, an authenticated caller's user_id flows
    in via the request context and resolves their own cookbook.
    """

    def test_no_cookbook_id_resolves_caller_user_cookbook(self, db_session):
        user, api_key = _make_user_and_key(db_session, "rec_charlie_token")
        cookbook = Bundle(
            id=uuid4(),
            name="Charlie's Cookbook",
            bundle_owner=user.id,
        )
        db_session.add(cookbook)
        skill = make_skill(db_session, slug="charlie-skill", title="Charlie Skill")
        db_session.add(
            BundleSkill(
                bundle_id=cookbook.id,
                skill_id=skill.id,
                source="custom-added",
            )
        )
        db_session.commit()

        server = build_mcp_server(db_factory=lambda: db_session)
        caller = {
            "scope": "operator",
            "user_id": user.id,
            "api_key_id": api_key.id,
        }
        payload = _drive_call_tool_with_caller(
            server, "recipes_list_cookbook", {}, caller
        )
        assert payload["cookbook"] is not None, (
            "Bug regression: caller's own cookbook should resolve from user_id"
        )
        assert payload["cookbook"]["id"] == str(cookbook.id)
        assert any(s["slug"] == "charlie-skill" for s in payload["skills"])

    def test_no_caller_falls_through_to_empty_cookbook(self, db_session):
        """The fallback (operator master, user_id=None) must still return the
        empty result for callers with no cookbook resolution path. This
        confirms the original behavior is unchanged for the master-key path.
        """
        server = build_mcp_server(db_factory=lambda: db_session)
        payload = _drive_call_tool(server, "recipes_list_cookbook", {})
        assert payload == {"cookbook": None, "skills": []}


# ── recipes_install — InstallEvent.api_key_id plumbing ────────────────────


class TestInstallRecordsApiKeyId:
    """``recipes_install`` records an InstallEvent. With auth plumbing fixed,
    the row must capture the authenticated caller's api_key_id.
    """

    def test_install_records_caller_api_key_id(self, db_session):
        user, api_key = _make_user_and_key(db_session, "rec_dave_token")
        skill = make_skill(db_session, slug="installable-skill", title="Installable")
        version = SkillVersion(
            id=uuid4(),
            skill_id=skill.id,
            semver="1.0.0",
            checksum_sha256="x" * 64,
            tarball_size_bytes=1024,
        )
        db_session.add(version)
        db_session.commit()
        # Capture identity columns before the dispatch — recipes_install
        # commits, which expires our ORM instances under SAVEPOINT isolation.
        api_key_id = api_key.id
        user_id = user.id

        server = build_mcp_server(db_factory=lambda: db_session)
        caller = {
            # Note: Phase B changed user key scope to "user" but install
            # test uses legacy caller dict; _ctx_from_caller maps "operator" → "master"
            # for backwards compat with the stdio fallback path. Use "user" for accuracy:
            "scope": "user",
            "user_id": user_id,
            "api_key_id": api_key_id,
        }
        payload = _drive_call_tool_with_caller(
            server, "recipes_install", {"slug": "installable-skill"}, caller
        )
        assert payload.get("slug") == "installable-skill"

        event = (
            db_session.query(InstallEvent)
            .filter(InstallEvent.skill_slug == "installable-skill")
            .one()
        )
        assert event.api_key_id == api_key_id, (
            "Bug regression: InstallEvent.api_key_id must reflect authenticated key"
        )

    def test_install_with_master_key_records_null_api_key_id(self, db_session):
        skill = make_skill(db_session, slug="master-installable", title="Master Installable")
        version = SkillVersion(
            id=uuid4(),
            skill_id=skill.id,
            semver="1.0.0",
            checksum_sha256="y" * 64,
            tarball_size_bytes=2048,
        )
        db_session.add(version)
        db_session.commit()

        server = build_mcp_server(db_factory=lambda: db_session)
        # Master operator scope — explicitly null user/key (the production
        # ASGI fast-path stashes exactly this dict when the master key is used).
        caller = {"scope": "operator", "user_id": None, "api_key_id": None}
        _drive_call_tool_with_caller(
            server, "recipes_install", {"slug": "master-installable"}, caller
        )

        event = (
            db_session.query(InstallEvent)
            .filter(InstallEvent.skill_slug == "master-installable")
            .one()
        )
        assert event.api_key_id is None


# ── _authenticate dependency stashes caller on request.state ──────────────


class TestAuthenticateStashesCallerOnRequestState:
    """The ``_authenticate`` SSE/messages dependency must stash the resolved
    caller on ``request.state.mcp_caller`` so the per-call dispatch can
    retrieve it via ``server.request_context``.
    """

    @pytest.fixture()
    def app(self, db_session):
        app = FastAPI()

        def override_get_db():
            try:
                yield db_session
            finally:
                pass

        app.include_router(mcp_router)
        app.dependency_overrides[get_db] = override_get_db
        return app

    def test_authenticate_master_key_stashes_caller(self, app, db_session):
        """Drive _authenticate via a probe endpoint that surfaces the stashed
        caller. Verifies the dependency runs and writes mcp_caller to state.
        """
        from app.mcp.server import _authenticate

        captured: dict[str, Any] = {}

        @app.get("/test_auth_probe")
        def probe(request: Request, auth=__import__("fastapi").Depends(_authenticate)):
            captured["mcp_caller"] = getattr(request.state, "mcp_caller", None)
            return {"ok": True}

        with TestClient(app) as client:
            resp = client.get(
                "/test_auth_probe",
                headers={"x-api-key": settings.API_KEY},
            )
        assert resp.status_code == 200
        # Phase B (Issue #5): master key now stashes scope='master'
        assert captured["mcp_caller"]["scope"] == "master"
        assert captured["mcp_caller"]["user_id"] is None
        assert captured["mcp_caller"]["api_key_id"] is None

    def test_authenticate_user_key_stashes_full_caller(self, app, db_session):
        from app.mcp.server import _authenticate

        user, api_key = _make_user_and_key(db_session, "rec_eve_token")
        db_session.commit()

        captured: dict[str, Any] = {}

        @app.get("/test_auth_probe_user")
        def probe(request: Request, auth=__import__("fastapi").Depends(_authenticate)):
            captured["mcp_caller"] = getattr(request.state, "mcp_caller", None)
            return {"ok": True}

        with TestClient(app) as client:
            resp = client.get(
                "/test_auth_probe_user",
                headers={"x-api-key": "rec_eve_token"},
            )
        assert resp.status_code == 200
        assert captured["mcp_caller"]["user_id"] == user.id
        assert captured["mcp_caller"]["api_key_id"] == api_key.id
        # Phase B (Issue #5): user key now stashes scope='user'
        assert captured["mcp_caller"]["scope"] == "user"


# ── Streamable HTTP ASGI wrapper stashes caller on scope["state"] ──────────


class TestStreamableHTTPAuthGateStashesCaller:
    """The /api/mcp/http ASGI wrapper must stash the caller dict on
    ``scope["state"]["mcp_caller"]`` so the session manager's downstream
    dispatch can pull it via the same request-context plumbing.
    """

    @pytest.fixture()
    def app(self, db_session):
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

        app.include_router(mcp_router)
        app.dependency_overrides[get_db] = override_get_db
        app.router.routes.append(_build_streamable_http_mount())

        @asynccontextmanager
        async def _lifespan(_app):
            from app.mcp.server import run_streamable_http

            async with run_streamable_http():
                yield

        app.router.lifespan_context = _lifespan
        return app

    def test_master_key_request_stashes_operator_caller(self, app, monkeypatch):
        """Patch the session manager's handle_request so we can capture the
        scope passed into it after the auth gate runs. This avoids needing
        a full MCP handshake just to verify state plumbing.
        """
        captured: dict[str, Any] = {}

        from app.mcp import server as server_mod

        original_get_mgr = server_mod.get_http_session_manager
        mgr = original_get_mgr()
        original_handle = mgr.handle_request

        async def capture_handle_request(scope, receive, send):
            captured["state"] = dict(scope.get("state") or {})
            # Short-circuit: respond 204 without running the actual MCP loop.
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        monkeypatch.setattr(mgr, "handle_request", capture_handle_request)

        with TestClient(app) as client:
            resp = client.post(
                "/api/mcp/http",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                headers={
                    "x-api-key": settings.API_KEY,
                    "Accept": "application/json, text/event-stream",
                },
            )
        assert resp.status_code == 204
        # Phase B (Issue #5): master key now stashes scope='master' in StreamableHTTP
        mc = captured["state"].get("mcp_caller")
        assert mc is not None
        assert mc["scope"] == "master"
        assert mc["user_id"] is None
        assert mc["api_key_id"] is None

    def test_user_key_request_stashes_full_caller(self, app, db_session, monkeypatch):
        user, api_key = _make_user_and_key(db_session, "rec_frank_token")
        db_session.commit()
        expected_user_id = user.id
        expected_api_key_id = api_key.id

        captured: dict[str, Any] = {}

        from app.mcp import server as server_mod

        # The ASGI wrapper opens a fresh SessionLocal() for non-master keys
        # because it executes outside FastAPI's dependency graph. In the
        # SQLite test environment we redirect that lookup to our shared
        # in-memory db_session via a context-managed shim.
        from contextlib import contextmanager

        @contextmanager
        def _session_shim():
            yield db_session

        # SessionLocal is constructed as a callable that returns a session;
        # its .close() must be a no-op so we don't kill the shared session.
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

        mgr = server_mod.get_http_session_manager()

        async def capture_handle_request(scope, receive, send):
            captured["state"] = dict(scope.get("state") or {})
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        monkeypatch.setattr(mgr, "handle_request", capture_handle_request)

        with TestClient(app) as client:
            resp = client.post(
                "/api/mcp/http",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                headers={
                    "x-api-key": "rec_frank_token",
                    "Accept": "application/json, text/event-stream",
                },
            )
        assert resp.status_code == 204
        stashed = captured["state"].get("mcp_caller")
        assert stashed is not None
        assert stashed["user_id"] == expected_user_id
        assert stashed["api_key_id"] == expected_api_key_id
        # Phase B (Issue #5): user key now stashes scope='user'
        assert stashed["scope"] == "user"

    def test_unauthorized_request_does_not_stash_caller(self, app, db_session, monkeypatch):
        """Unauthorized requests must short-circuit at the gate and never
        forward to the session manager — guarding against state leaks.
        """
        from app.mcp import server as server_mod

        # Same SessionLocal shim as above so the rec_-prefixed lookup
        # doesn't hit Postgres in the test environment.
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

        mgr = server_mod.get_http_session_manager()
        called = {"forwarded": False}

        async def capture_handle_request(scope, receive, send):
            called["forwarded"] = True
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        monkeypatch.setattr(mgr, "handle_request", capture_handle_request)

        with TestClient(app) as client:
            resp = client.post(
                "/api/mcp/http",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                headers={
                    "x-api-key": "rec_does_not_exist",
                    "Accept": "application/json, text/event-stream",
                },
            )
        assert resp.status_code == 401
        assert called["forwarded"] is False, (
            "Unauthorized requests must not reach the session manager"
        )


# ── recipes_sync caller plumbing ────────────────────────────────────────


class TestRecipesSyncReceivesCaller:
    """``recipes_sync`` accepts a ``caller`` kwarg. After the fix, that
    kwarg must reflect the authenticated user — not the hardcoded master.
    """

    def test_sync_receives_ctx_from_request_context(self, db_session):
        """Phase B (Issue #15): recipes_sync now takes ctx= (AuthContext),
        not caller=. Verify the AuthContext is reconstructed from the caller
        dict and passed correctly.
        """
        captured: dict[str, Any] = {}

        from app.auth_ctx import AuthContext
        from app.mcp import server as server_mod

        def fake_sync(db, *, cookbook_id, dry_run=False, caller=None, ctx=None):
            captured["ctx"] = ctx
            captured["dry_run"] = dry_run
            return {"applied": False, "changes": []}

        with patch.object(server_mod, "recipes_sync", fake_sync):
            server = build_mcp_server(db_factory=lambda: db_session)
            user, api_key = _make_user_and_key(db_session, "rec_sync_token")
            caller = {
                "scope": "user",
                "user_id": user.id,
                "api_key_id": api_key.id,
            }
            _drive_call_tool_with_caller(
                server,
                "recipes_sync",
                {"cookbook_id": str(uuid4()), "dry_run": True},
                caller,
            )
        assert captured["ctx"] is not None, "ctx must be passed to recipes_sync"
        assert isinstance(captured["ctx"], AuthContext), (
            f"ctx must be AuthContext, got {type(captured['ctx'])}"
        )
        assert captured["ctx"].scope == "user"
        assert captured["ctx"].user_id == user.id
        assert captured["dry_run"] is True


# ── tools/list still works in 10-tool catalogue ─────────────────────────


def test_tool_catalogue_unchanged():
    """RCP-10 must not shrink the tool surface below the v1 contract.

    Original constant was ``== 10`` (Phase A baseline). The catalogue has
    grown to 14 as new MCP tools shipped (recipes_doctor, recipes_feedback,
    recipes_carousel_today, recipes_propose_skill_patch). Pin the floor so
    external MCP clients keep working; track growth in source-control diffs.
    """
    tools = _tool_definitions()
    assert len(tools) >= 10
    names = {t.name for t in tools}
    PHASE_A_REQUIRED = {"recipes_search", "recipes_install", "recipes_recall"}
    assert PHASE_A_REQUIRED.issubset(names), (
        f"Phase A tool contract broken — missing {PHASE_A_REQUIRED - names}"
    )
