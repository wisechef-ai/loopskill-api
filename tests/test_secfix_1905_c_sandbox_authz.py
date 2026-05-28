"""Tests for sandbox authz gate — Issue authz tightening.

Acceptance gates:
  - user-scope key on POST /api/sandbox/run → 403
  - master-scope key → allowed (200 or 4xx from business logic, not 403)
  - user-scope key with is_sandbox_operator=True → allowed
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth_ctx import AuthContext
from app.authz import can_run_sandbox

pytestmark = [pytest.mark.sandbox_linux_only]


# ---------------------------------------------------------------------------
# Unit tests for can_run_sandbox predicate (authz.py)
# ---------------------------------------------------------------------------

def test_master_scope_can_run_sandbox():
    ctx = AuthContext(scope="master")
    assert can_run_sandbox(ctx) is True


def test_user_scope_cannot_run_sandbox_by_default():
    from uuid import uuid4
    ctx = AuthContext(scope="user", user_id=uuid4())
    assert can_run_sandbox(ctx) is False


def test_user_scope_with_is_sandbox_operator_can_run():
    from uuid import uuid4
    ctx = AuthContext(scope="user", user_id=uuid4(), is_sandbox_operator=True)
    assert can_run_sandbox(ctx) is True


def test_anonymous_cannot_run_sandbox():
    ctx = AuthContext.anonymous()
    assert can_run_sandbox(ctx) is False


def test_operator_scope_without_flag_cannot_run():
    from uuid import uuid4
    ctx = AuthContext(scope="operator", user_id=uuid4(), is_sandbox_operator=False)
    assert can_run_sandbox(ctx) is False


# ---------------------------------------------------------------------------
# Integration-style tests via TestClient
# ---------------------------------------------------------------------------

def _make_app_with_auth(auth_ctx: AuthContext) -> FastAPI:
    """Create a minimal FastAPI app that stamps a fixed AuthContext and includes sandbox routes."""
    from fastapi import FastAPI
    from app.sandbox.routes import router

    app = FastAPI()

    @app.middleware("http")
    async def stamp_auth(request: Request, call_next):
        request.state.auth_ctx = auth_ctx
        return await call_next(request)

    app.include_router(router)
    return app


def _user_ctx(is_sandbox_operator: bool = False) -> AuthContext:
    from uuid import uuid4
    return AuthContext(
        scope="user",
        user_id=uuid4(),
        api_key_id=uuid4(),
        is_sandbox_operator=is_sandbox_operator,
    )


def _master_ctx() -> AuthContext:
    return AuthContext(scope="master")


def _post_sandbox_run(client: TestClient) -> int:
    resp = client.post("/api/skills/test-skill/sandbox/run", json={})
    return resp.status_code


@pytest.fixture()
def skill_mock(tmp_path):
    """Patch DB and runner so routes can reach the authz check."""
    version = MagicMock()
    version.semver = "1.0.0"
    version.skill_toml = (
        '[meta]\nslug = "test-skill"\n\n'
        "[sandbox]\nnetwork_allow = []\nmemory_mb = 256\ntimeout_seconds = 60\n"
    )
    version.tarball_path = None

    skill = MagicMock()
    skill.slug = "test-skill"
    skill.versions = [version]

    skill_dir = str(tmp_path / "skill")
    import os; os.makedirs(skill_dir)
    (tmp_path / "skill" / "setup.sh").write_text("#!/bin/bash\necho hi\n")

    runner_result = MagicMock()
    runner_result.sandbox_id = "abc"
    runner_result.exit_code = 0
    runner_result.stdout = ""
    runner_result.stderr = ""
    runner_result.timed_out = False
    runner_result.duration_seconds = 0.1
    runner_result.success = True
    runner_result.error = None

    with patch("app.sandbox.routes.get_db") as mock_get_db, \
         patch("app.sandbox.routes.get_runner") as mock_get_runner, \
         patch("app.sandbox.routes._resolve_skill_dir", return_value=skill_dir):

        mock_db = MagicMock()
        mock_db.query.return_value.options.return_value.filter.return_value.first.return_value = skill
        mock_get_db.return_value = iter([mock_db])

        mock_runner = MagicMock()
        mock_runner.run.return_value = runner_result
        mock_get_runner.return_value = mock_runner

        yield


def test_user_scope_gets_403(skill_mock):
    """User-scope key without is_sandbox_operator → 403 Forbidden."""
    app = _make_app_with_auth(_user_ctx(is_sandbox_operator=False))
    client = TestClient(app, raise_server_exceptions=False)
    status = _post_sandbox_run(client)
    assert status == 403, f"Expected 403, got {status}"


def test_master_scope_passes_authz(skill_mock):
    """Master-scope key → not 403 (may be 200 or 4xx from business logic)."""
    app = _make_app_with_auth(_master_ctx())
    client = TestClient(app, raise_server_exceptions=False)
    status = _post_sandbox_run(client)
    assert status != 403, f"Expected master to pass authz (not 403), got {status}"


def test_user_with_sandbox_operator_flag_passes_authz(skill_mock):
    """User-scope key with is_sandbox_operator=True → not 403."""
    app = _make_app_with_auth(_user_ctx(is_sandbox_operator=True))
    client = TestClient(app, raise_server_exceptions=False)
    status = _post_sandbox_run(client)
    assert status != 403, (
        f"Expected is_sandbox_operator=True to bypass authz block, got {status}"
    )
