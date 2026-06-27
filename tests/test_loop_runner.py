"""Tests for the loop RUNNER — POST /api/loops/{slug}/run and app.loop_runner.

loopskill_run_0627. The runner makes the vetted loop registry *executable*: it
runs a published loop's verification_script under enforced bounds and returns an
objective pass/fail, declaring the confinement level it achieved.

Two layers tested:
  - unit: app.loop_runner pure helpers + LoopRunner.run_verification (bounded mode,
    which CI exercises for real since CI has no kernel sandbox backend).
  - route: POST /api/loops/{slug}/run auth gate, mode dispatch, 404, and a real
    end-to-end verify pass + fail against a published loop.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import loop_runner as lr
from app.auth_ctx import AuthContext
from app.database import get_db
from app.loop_routes import router as loop_router


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def app_client(db_session):
    """App with the loop router + a stub auth middleware (mirrors APIKeyMiddleware).

    x-test-auth: user  -> authenticated user
    x-test-auth: master -> master scope
    (absent)           -> anonymous
    """
    app = FastAPI()

    @app.middleware("http")
    async def _stub_auth(request: Request, call_next):
        hdr = request.headers.get("x-test-auth")
        if hdr == "user":
            request.state.auth_ctx = AuthContext(scope="user", user_id=uuid4())
        elif hdr == "master":
            request.state.auth_ctx = AuthContext(scope="master")
        else:
            request.state.auth_ctx = AuthContext.anonymous()
        return await call_next(request)

    app.include_router(loop_router)

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, raise_server_exceptions=True)


def _publish(client: TestClient, *, slug: str, verification_script: str) -> None:
    """Publish a minimal valid loop with the given verification script."""
    payload = {
        "slug": slug,
        "title": f"Loop {slug}",
        "description": "test loop",
        "success_condition": "the check passes",
        "verification_script": verification_script,
        "system_prompt": "do the thing",
        "max_turns": 5,
        "budget_usd": None,
        "tool_allowlist": ["file_write"],
        "stopping_criteria": {"success": "exit 0", "failure": "max_turns", "budget": "n/a"},
    }
    resp = client.post("/api/loops", json=payload, headers={"x-test-auth": "user"})
    assert resp.status_code == 201, resp.text


# ── unit: pure helpers ───────────────────────────────────────────────────────


class TestSafeWorkspacePath:
    def test_rejects_absolute(self):
        assert lr._safe_workspace_path("/etc/passwd") is None

    def test_rejects_parent_traversal(self):
        assert lr._safe_workspace_path("../escape.txt") is None
        assert lr._safe_workspace_path("a/../../escape.txt") is None

    def test_rejects_home_expansion(self):
        assert lr._safe_workspace_path("~/secrets") is None

    def test_rejects_reserved_verify_script_name(self):
        assert lr._safe_workspace_path(lr.VERIFY_SCRIPT_NAME) is None
        assert lr._safe_workspace_path("sub/" + lr.VERIFY_SCRIPT_NAME) is None

    def test_rejects_empty(self):
        assert lr._safe_workspace_path("") is None
        assert lr._safe_workspace_path("   ") is None

    def test_allows_simple_relative(self):
        assert lr._safe_workspace_path("artifact.txt") == "artifact.txt"
        assert lr._safe_workspace_path("sub/dir/file.json") == "sub/dir/file.json"


class TestScrubEnv:
    def test_server_secrets_never_inherited(self, monkeypatch):
        # A server secret in os.environ must NOT appear in the scrubbed env.
        monkeypatch.setenv("WR_MASTER_KEY", "super-secret")
        monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_x")
        env = lr._scrub_env(None, "/tmp/workdir")
        assert "WR_MASTER_KEY" not in env
        assert "STRIPE_SECRET_KEY" not in env

    def test_caller_vars_pass_through(self):
        env = lr._scrub_env({"PR_NUMBER": "42", "FOO": "bar"}, "/tmp/workdir")
        assert env["PR_NUMBER"] == "42"
        assert env["FOO"] == "bar"

    def test_caller_cannot_override_path_or_home(self):
        env = lr._scrub_env(
            {"PATH": "/evil/bin", "HOME": "/root", "LD_PRELOAD": "/evil.so"},
            "/tmp/workdir",
        )
        assert env["PATH"] != "/evil/bin"
        assert env["HOME"] == "/tmp/workdir"
        assert "LD_PRELOAD" not in env

    def test_non_string_values_dropped(self):
        env = lr._scrub_env({"GOOD": "ok", "BAD": 123}, "/tmp/workdir")  # type: ignore[dict-item]
        assert env["GOOD"] == "ok"
        assert "BAD" not in env


class TestClampInt:
    def test_none_returns_default(self):
        assert lr._clamp_int(None, 60, 1, 600) == 60

    def test_clamps_high(self):
        assert lr._clamp_int(99999, 60, 1, 600) == 600

    def test_clamps_low(self):
        assert lr._clamp_int(0, 60, 1, 600) == 1

    def test_invalid_returns_default(self):
        assert lr._clamp_int("nope", 60, 1, 600) == 60  # type: ignore[arg-type]


# ── unit: LoopRunner.run_verification (bounded mode, real execution) ──────────


@pytest.fixture()
def runner(tmp_path):
    """A LoopRunner forced into bounded mode so CI exercises the no-backend path.

    CI has no firejail/bwrap; forcing 'none' here makes the assertion deterministic
    regardless of the host's installed backends.
    """
    r = lr.LoopRunner(workspace_base=str(tmp_path))
    r._backend = "none"
    return r


class TestRunVerificationBounded:
    def test_passing_script_passes(self, runner):
        res = runner.run_verification(
            loop_slug="ok-loop",
            verification_script="exit 0",
            declared_bounds={"max_turns": 5},
        )
        assert res.passed is True
        assert res.exit_code == 0
        assert res.confinement == "bounded"
        assert res.timed_out is False

    def test_failing_script_fails(self, runner):
        res = runner.run_verification(
            loop_slug="bad-loop",
            verification_script="exit 7",
            declared_bounds={},
        )
        assert res.passed is False
        assert res.exit_code == 7

    def test_workspace_file_is_checkable(self, runner):
        # The script checks a caller-staged file — the real verify use case.
        res = runner.run_verification(
            loop_slug="file-loop",
            verification_script="test -f artifact.txt && grep -q hello artifact.txt",
            declared_bounds={},
            workspace_files={"artifact.txt": "hello world"},
        )
        assert res.passed is True

    def test_timeout_is_enforced(self, runner):
        res = runner.run_verification(
            loop_slug="slow-loop",
            verification_script="sleep 30",
            declared_bounds={},
            timeout_seconds=1,
        )
        assert res.timed_out is True
        assert res.passed is False

    def test_declared_bounds_echoed(self, runner):
        res = runner.run_verification(
            loop_slug="bounds-loop",
            verification_script="exit 0",
            declared_bounds={"max_turns": 10, "tool_allowlist": ["github_read_pr"]},
        )
        assert res.bounds["max_turns"] == 10
        assert res.bounds["tool_allowlist"] == ["github_read_pr"]
        assert res.bounds["run_timeout_seconds"] >= 1
        assert res.bounds["network"] is False

    def test_server_env_not_visible_to_script(self, runner, monkeypatch):
        # End-to-end: a server secret in os.environ must not leak into the run.
        monkeypatch.setenv("WR_SUPER_SECRET", "leak-me-not")
        res = runner.run_verification(
            loop_slug="env-loop",
            verification_script='test -z "$WR_SUPER_SECRET"',
            declared_bounds={},
        )
        assert res.passed is True, f"server secret leaked into run: {res.stdout} {res.stderr}"

    def test_unsafe_workspace_path_rejected(self, runner):
        res = runner.run_verification(
            loop_slug="evil-loop",
            verification_script="exit 0",
            declared_bounds={},
            workspace_files={"../escape.txt": "x"},
        )
        assert res.passed is False
        assert res.error is not None and "unsafe" in res.error

    def test_too_many_workspace_files_rejected(self, runner):
        files = {f"f{i}.txt": "x" for i in range(lr.MAX_WORKSPACE_FILES + 1)}
        res = runner.run_verification(
            loop_slug="many-loop",
            verification_script="exit 0",
            declared_bounds={},
            workspace_files=files,
        )
        assert res.passed is False
        assert res.error is not None and "too many" in res.error

    def test_empty_verification_script_fails_closed(self, runner):
        res = runner.run_verification(
            loop_slug="empty-loop",
            verification_script="   ",
            declared_bounds={},
        )
        assert res.passed is False
        assert res.error is not None


# ── route: POST /api/loops/{slug}/run ────────────────────────────────────────


class TestRunRoute:
    def test_requires_auth(self, app_client):
        _publish(app_client, slug="auth-loop", verification_script="exit 0")
        resp = app_client.post("/api/loops/auth-loop/run", json={})  # no auth header
        assert resp.status_code == 401

    def test_404_for_unknown_loop(self, app_client):
        resp = app_client.post("/api/loops/does-not-exist/run", json={}, headers={"x-test-auth": "user"})
        assert resp.status_code == 404

    def test_agent_mode_returns_501(self, app_client):
        _publish(app_client, slug="agent-loop", verification_script="exit 0")
        resp = app_client.post(
            "/api/loops/agent-loop/run",
            json={"mode": "agent"},
            headers={"x-test-auth": "user"},
        )
        assert resp.status_code == 501

    def test_unknown_mode_returns_422(self, app_client):
        _publish(app_client, slug="weird-loop", verification_script="exit 0")
        resp = app_client.post(
            "/api/loops/weird-loop/run",
            json={"mode": "teleport"},
            headers={"x-test-auth": "user"},
        )
        assert resp.status_code == 422

    def test_verify_pass_end_to_end(self, app_client):
        _publish(app_client, slug="pass-loop", verification_script="exit 0")
        resp = app_client.post("/api/loops/pass-loop/run", json={}, headers={"x-test-auth": "user"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["passed"] is True
        assert body["loop_slug"] == "pass-loop"
        assert body["mode"] == "verify"
        assert body["confinement"] in ("sandboxed", "bounded")

    def test_verify_fail_end_to_end(self, app_client):
        _publish(app_client, slug="fail-loop", verification_script="exit 3")
        resp = app_client.post("/api/loops/fail-loop/run", json={}, headers={"x-test-auth": "user"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["passed"] is False
        assert body["exit_code"] == 3

    def test_verify_with_workspace_file(self, app_client):
        _publish(
            app_client,
            slug="ws-loop",
            verification_script="test -f out.txt && grep -q DONE out.txt",
        )
        resp = app_client.post(
            "/api/loops/ws-loop/run",
            json={"workspace_files": {"out.txt": "DONE"}},
            headers={"x-test-auth": "user"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["passed"] is True

    def test_master_scope_can_run(self, app_client):
        _publish(app_client, slug="master-loop", verification_script="exit 0")
        resp = app_client.post("/api/loops/master-loop/run", json={}, headers={"x-test-auth": "master"})
        assert resp.status_code == 200
        assert resp.json()["passed"] is True
