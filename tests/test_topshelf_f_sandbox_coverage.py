"""Phase F — coverage push for app/sandbox modules.

Targets uncovered areas:
  - runner.py: firejail run (success + timeout + staging failure), bwrap run, 
               _start_domain_proxy_sync (port-timeout, bad port),
               _stop_domain_proxy_sync (error handling), _parse_firejail_output,
               _prepare_bwrap_root, _cleanup
  - routes.py: GET status (skill not found, private skill, no toml, ValueError profile,
               sandbox block check), POST run (skill not found, private archived,
               version pinning, no toml, dangerous profile, no skill dir)
  - domain_proxy.py: DomainProxy start/stop, _handle_connect (deny, error, CONNECT OK),
                     _handle_http, _domain_matches, run_domain_proxy
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth_ctx import AuthContext
from app.models import Base, Skill, SkillVersion
from app.sandbox.domain_proxy import DomainProxy, _domain_matches, run_domain_proxy
from app.sandbox.profile import SandboxProfile
from app.sandbox.runner import SandboxResult, SandboxRunner, SandboxError

pytestmark = [pytest.mark.sandbox_linux_only]


# ─── Unit: _domain_matches ────────────────────────────────────────────────────


class TestDomainMatches:

    def test_exact_match(self):
        assert _domain_matches("api.github.com", ["api.github.com"]) is True

    def test_subdomain_match(self):
        """github.com allows sub.github.com (line 58)."""
        assert _domain_matches("sub.github.com", ["github.com"]) is True

    def test_no_match(self):
        assert _domain_matches("evil.com", ["api.github.com"]) is False

    def test_empty_allowed_list(self):
        assert _domain_matches("anything.com", []) is False

    def test_case_insensitive(self):
        """Hostname comparison is case-insensitive (line 52-54)."""
        assert _domain_matches("API.GITHUB.COM", ["api.github.com"]) is True

    def test_multiple_patterns_first_match(self):
        assert _domain_matches("pypi.org", ["npmjs.org", "pypi.org"]) is True


# ─── Unit: DomainProxy ────────────────────────────────────────────────────────


class TestDomainProxy:

    @pytest.mark.asyncio
    async def test_start_returns_port(self):
        """DomainProxy.start() returns a valid port number (lines 73-84)."""
        proxy = DomainProxy(allowed_domains=["api.github.com"])
        port = await proxy.start()
        assert isinstance(port, int)
        assert port > 0
        assert proxy.port == port
        await proxy.stop()

    @pytest.mark.asyncio
    async def test_stop_with_no_server(self):
        """DomainProxy.stop() is safe when not started (line 86-96)."""
        proxy = DomainProxy(allowed_domains=[])
        await proxy.stop()  # should not raise

    @pytest.mark.asyncio
    async def test_stop_cancels_connections(self):
        """DomainProxy.stop() cancels active tasks (lines 92-95)."""
        proxy = DomainProxy(allowed_domains=["test.com"])
        await proxy.start()

        # Create a real asyncio task that we can cancel
        async def dummy_coro():
            await asyncio.sleep(100)

        task = asyncio.ensure_future(dummy_coro())
        proxy._connections.add(task)
        await proxy.stop()
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_proxy_connection_denied_connect(self):
        """CONNECT to disallowed domain → 403 (lines 168-172)."""
        proxy = DomainProxy(allowed_domains=["allowed.com"])
        await proxy.start()

        reader = asyncio.StreamReader()
        reader.feed_data(b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com\r\n\r\n")

        output = bytearray()

        class MockWriter:
            def write(self, data):
                output.extend(data)

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        writer = MockWriter()
        await proxy._proxy_connection(reader, writer)
        assert b"403" in bytes(output)

    @pytest.mark.asyncio
    async def test_proxy_connection_empty_request(self):
        """Empty request → silently returns (line 125-126)."""
        proxy = DomainProxy(allowed_domains=[])
        reader = asyncio.StreamReader()
        reader.feed_eof()  # empty

        output = bytearray()

        class MockWriter:
            def write(self, data):
                output.extend(data)

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        await proxy._proxy_connection(reader, MockWriter())
        # No crash, no output
        assert len(output) == 0

    @pytest.mark.asyncio
    async def test_handle_connect_invalid_port(self):
        """CONNECT target:notaport → deny (lines 150-153)."""
        proxy = DomainProxy(allowed_domains=["allowed.com"])
        await proxy.start()

        reader = asyncio.StreamReader()
        reader.feed_data(b"\r\n")  # empty headers

        output = bytearray()

        class MockWriter:
            def write(self, data):
                output.extend(data)

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        await proxy._handle_connect("allowed.com:notaport", reader, MockWriter())
        assert b"403" in bytes(output)

    @pytest.mark.asyncio
    async def test_handle_connect_no_port_defaults_443(self):
        """CONNECT target without port defaults to 443 (line 155-156)."""
        proxy = DomainProxy(allowed_domains=["allowed.com"])
        await proxy.start()

        reader = asyncio.StreamReader()
        reader.feed_data(b"\r\n")

        output = bytearray()
        connect_called_with = {}

        class MockWriter:
            def write(self, data):
                output.extend(data)

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        # Mock open_connection to capture the port
        with patch("asyncio.open_connection") as mock_conn:
            mock_conn.side_effect = OSError("connection refused")
            await proxy._handle_connect("allowed.com", reader, MockWriter())
        # Should attempt connection to port 443
        mock_conn.assert_called_once()
        args = mock_conn.call_args
        assert args[0][1] == 443 or args[1].get("port", args[0][1] if len(args[0]) > 1 else None) == 443

    @pytest.mark.asyncio
    async def test_handle_http_denied(self):
        """HTTP to disallowed domain → 403 (lines 223-227)."""
        proxy = DomainProxy(allowed_domains=["allowed.com"])
        reader = asyncio.StreamReader()
        reader.feed_data(b"Host: evil.com\r\n\r\n")

        output = bytearray()

        class MockWriter:
            def write(self, data):
                output.extend(data)

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        await proxy._handle_http("GET / HTTP/1.1", "http://evil.com/", reader, MockWriter())
        assert b"403" in bytes(output)

    @pytest.mark.asyncio
    async def test_handle_http_uri_fallback_hostname(self):
        """HTTP with no Host header uses URI for hostname (lines 216-221)."""
        proxy = DomainProxy(allowed_domains=["evil.com"])
        reader = asyncio.StreamReader()
        reader.feed_data(b"\r\n")  # no headers

        output = bytearray()
        connect_called = {}

        class MockWriter:
            def write(self, data):
                output.extend(data)

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        # evil.com is allowed → should attempt upstream connection
        with patch("asyncio.open_connection") as mock_conn:
            mock_conn.side_effect = OSError("refused")
            await proxy._handle_http("GET / HTTP/1.1", "http://evil.com/path", reader, MockWriter())
        # Should return 502 (upstream failed)
        assert b"502" in bytes(output)

    @pytest.mark.asyncio
    async def test_run_domain_proxy_function(self):
        """run_domain_proxy creates and starts a proxy (line 284-288)."""
        proxy = await run_domain_proxy(["api.github.com"])
        assert proxy.port is not None
        assert proxy.port > 0
        await proxy.stop()

    @pytest.mark.asyncio
    async def test_handle_client_exception_suppressed(self):
        """Exceptions in _handle_client are suppressed (line 107-108)."""
        proxy = DomainProxy(allowed_domains=[])
        await proxy.start()

        # Simulate a client connection that raises
        reader = asyncio.StreamReader()
        reader.feed_data(b"GARBAGE DATA THAT IS INVALID\r\n\r\n")

        class FailWriter:
            def write(self, data):
                raise RuntimeError("writer failed")

            async def drain(self):
                pass

            def close(self):
                pass

            async def wait_closed(self):
                pass

        # Should not propagate exception
        await proxy._handle_client(reader, FailWriter())
        await proxy.stop()


# ─── Unit: SandboxRunner extended ─────────────────────────────────────────────


class TestSandboxRunnerExtended:
    """Cover branches in runner.py not covered by existing tests."""

    def setup_method(self):
        self.workspace = tempfile.mkdtemp()
        self.runner = SandboxRunner(workspace=self.workspace)

    def teardown_method(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def _make_skill_dir(self):
        d = tempfile.mkdtemp(prefix="skill_")
        ep = os.path.join(d, "setup.sh")
        with open(ep, "w") as f:
            f.write("#!/bin/bash\necho ok\n")
        os.chmod(ep, 0o755)
        return d

    def test_firejail_success_path(self, tmp_path):
        """Firejail run completes normally (lines 274-300)."""
        self.runner._backend = "firejail"
        skill_dir = self._make_skill_dir()
        profile = SandboxProfile(network_allow=[])

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"ok\n"
        mock_result.stderr = b""

        with patch("subprocess.run", return_value=mock_result):
            with patch("shutil.copytree"):
                with patch("os.chmod"):
                    result = self.runner.run(skill_dir, "setup.sh", profile)

        assert result.exit_code == 0
        assert result.error is None
        shutil.rmtree(skill_dir, ignore_errors=True)

    def test_firejail_timeout_expired(self, tmp_path):
        """subprocess.TimeoutExpired → SandboxResult(timed_out=True) (lines 302-314)."""
        self.runner._backend = "firejail"
        skill_dir = self._make_skill_dir()
        profile = SandboxProfile(network_allow=[], timeout_seconds=1)

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["firejail"], 1, output=b"partial", stderr=b"")):
            with patch("shutil.copytree"):
                with patch("os.chmod"):
                    result = self.runner.run(skill_dir, "setup.sh", profile)

        assert result.timed_out is True
        assert result.exit_code == -1
        shutil.rmtree(skill_dir, ignore_errors=True)

    def test_firejail_staging_failure(self, tmp_path):
        """shutil.copytree fails → SandboxResult with staging error (lines 240-249)."""
        self.runner._backend = "firejail"
        skill_dir = self._make_skill_dir()
        profile = SandboxProfile(network_allow=[])

        with patch("shutil.copytree", side_effect=OSError("no space left")):
            result = self.runner.run(skill_dir, "setup.sh", profile)

        assert result.exit_code == -1
        assert result.error is not None and "Staging failed" in result.error
        shutil.rmtree(skill_dir, ignore_errors=True)

    def test_firejail_with_proxy_env_injection(self, tmp_path):
        """Proxy port is injected into env vars (lines 258-264)."""
        self.runner._backend = "firejail"
        skill_dir = self._make_skill_dir()
        profile = SandboxProfile(network_allow=["api.github.com"])

        mock_proxy = {"process": MagicMock(), "port": 9876}
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"ok\n"
        mock_result.stderr = b""

        captured_cmd = {}

        def mock_run(cmd, **kwargs):
            captured_cmd["cmd"] = cmd
            return mock_result

        with patch.object(self.runner.__class__, "_start_domain_proxy_sync", return_value=mock_proxy):
            with patch.object(self.runner.__class__, "_stop_domain_proxy_sync"):
                with patch("subprocess.run", side_effect=mock_run):
                    with patch("shutil.copytree"):
                        with patch("os.chmod"):
                            result = self.runner.run(skill_dir, "setup.sh", profile, env={"MY_VAR": "val"})

        # Check proxy env vars were injected in the firejail command
        cmd_str = " ".join(captured_cmd.get("cmd", []))
        assert "http_proxy" in cmd_str or "9876" in cmd_str
        shutil.rmtree(skill_dir, ignore_errors=True)

    def test_bwrap_success_path(self, tmp_path):
        """bwrap run completes normally (lines 382-399)."""
        self.runner._backend = "bwrap"
        skill_dir = self._make_skill_dir()
        profile = SandboxProfile(network_allow=[], timeout_seconds=30)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"hello\n"
        mock_result.stderr = b""

        with patch("subprocess.run", return_value=mock_result):
            result = self.runner.run(skill_dir, "setup.sh", profile)

        assert result.exit_code == 0
        shutil.rmtree(skill_dir, ignore_errors=True)

    def test_bwrap_timeout_expired(self, tmp_path):
        """bwrap subprocess.TimeoutExpired → timed_out=True (lines 401-413)."""
        self.runner._backend = "bwrap"
        skill_dir = self._make_skill_dir()
        profile = SandboxProfile(network_allow=[], timeout_seconds=1)

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["bwrap"], 1, output=b"", stderr=b"")):
            result = self.runner.run(skill_dir, "setup.sh", profile)

        assert result.timed_out is True
        shutil.rmtree(skill_dir, ignore_errors=True)

    def test_parse_firejail_output_filters_status_lines(self):
        """_parse_firejail_output strips firejail status lines (lines 489-503)."""
        raw = b"Parent pid 1234\nChild process initialized\nParent is shutting down\nActual output\n"
        result = SandboxRunner._parse_firejail_output(raw)
        assert "Parent pid" not in result
        assert "Child process initialized" not in result
        assert "Actual output" in result

    def test_start_domain_proxy_sync_no_script(self, tmp_path):
        """Proxy script not found → RuntimeError (lines 428-429)."""
        with patch("os.path.exists", return_value=False):
            with pytest.raises(RuntimeError, match="Proxy script not found"):
                SandboxRunner._start_domain_proxy_sync(["test.com"])

    def test_start_domain_proxy_sync_timeout(self, tmp_path):
        """Proxy doesn't emit port within 5s → SandboxError (lines 450-462)."""
        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read.return_value = b"proxy crashed"

        with patch("os.path.exists", return_value=True):
            with patch("subprocess.Popen", return_value=mock_proc):
                # select never returns stdout as readable (timeout path)
                with patch("select.select", return_value=([], [], [])):
                    import time
                    with patch("time.monotonic", side_effect=[0.0, 6.0, 6.1]):  # immediate timeout
                        with pytest.raises(SandboxError, match="proxy did not emit port"):
                            SandboxRunner._start_domain_proxy_sync(["test.com"])

    def test_start_domain_proxy_sync_bad_port(self, tmp_path):
        """Proxy emits non-integer port → SandboxError (lines 464-468)."""
        mock_proc = MagicMock()
        stdout_mock = MagicMock()
        stdout_mock.readline.return_value = b"notaport\n"
        mock_proc.stdout = stdout_mock
        mock_proc.stderr = MagicMock()

        with patch("os.path.exists", return_value=True):
            with patch("subprocess.Popen", return_value=mock_proc):
                with patch("select.select", return_value=([stdout_mock], [], [])):
                    with pytest.raises(SandboxError, match="bad port"):
                        SandboxRunner._start_domain_proxy_sync(["test.com"])

    def test_stop_domain_proxy_sync_terminate_error(self):
        """terminate() raises → falls back to kill() (lines 481-486)."""
        mock_proc = MagicMock()
        mock_proc.terminate.side_effect = OSError("already gone")
        mock_proc.kill.side_effect = ProcessLookupError("already gone")
        # Should not raise
        SandboxRunner._stop_domain_proxy_sync({"process": mock_proc})

    def test_stop_domain_proxy_sync_no_process(self):
        """No process key → no-op (lines 475-476)."""
        SandboxRunner._stop_domain_proxy_sync({})  # should not raise

    def test_prepare_bwrap_root_copies_files(self, tmp_path):
        """_prepare_bwrap_root copies non-underscore files (lines 505-526)."""
        skill_dir = tmp_path / "skill"
        skill_dir.mkdir()
        (skill_dir / "setup.sh").write_text("#!/bin/bash\n")
        (skill_dir / "_private").write_text("skip me")
        (skill_dir / "subdir").mkdir()
        (skill_dir / "subdir" / "file.txt").write_text("sub")

        sandbox_root = tmp_path / "sandbox"
        sandbox_root.mkdir()
        (sandbox_root / "_tmp").mkdir()
        (sandbox_root / "_writable").mkdir()

        profile = SandboxProfile(fs_write=["/data"])
        self.runner._prepare_bwrap_root(str(skill_dir), str(sandbox_root), profile)

        assert (sandbox_root / "setup.sh").exists()
        assert not (sandbox_root / "_private").exists()
        assert (sandbox_root / "subdir").is_dir()

    def test_cleanup_nonexistent_path(self):
        """_cleanup with non-existent path doesn't crash (line 531)."""
        self.runner._cleanup("/tmp/does_not_exist_xyz_abc_123")

    def test_run_exception_returns_error_result(self, tmp_path):
        """Exception in _run_firejail → SandboxResult with error (lines 178-188)."""
        self.runner._backend = "firejail"
        skill_dir = self._make_skill_dir()
        profile = SandboxProfile(network_allow=[])

        with patch.object(self.runner, "_run_firejail", side_effect=RuntimeError("unexpected crash")):
            result = self.runner.run(skill_dir, "setup.sh", profile)

        assert result.exit_code == -1
        assert result.error is not None and "Execution failed" in result.error
        shutil.rmtree(skill_dir, ignore_errors=True)


# ─── Integration: sandbox routes ──────────────────────────────────────────────


def _make_sandbox_app(auth_ctx: AuthContext):
    """Build a minimal app with sandbox routes and a fixed auth_ctx."""
    from fastapi import FastAPI, Request
    from starlette.middleware.base import BaseHTTPMiddleware
    from app.sandbox.routes import router
    from app.database import get_db

    app = FastAPI()

    @app.middleware("http")
    async def stamp_auth(request: Request, call_next):
        request.state.auth_ctx = auth_ctx
        return await call_next(request)

    app.include_router(router)
    return app


@pytest.fixture(scope="module")
def engine_f():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def sp(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def sandbox_db(engine_f):
    conn = engine_f.connect()
    txn = conn.begin()
    Session = sessionmaker(bind=conn)
    session = Session()
    nested = conn.begin_nested()

    @sa_event.listens_for(session, "after_transaction_end")
    def restart(s, t):
        nonlocal nested
        if not nested.is_active:
            nested = conn.begin_nested()

    yield session
    session.close()
    txn.rollback()
    conn.close()


def _make_skill_row(db, slug: str, is_public: bool = True, is_archived: bool = False):
    from datetime import datetime, timezone
    from app.models import Skill
    sk = Skill(
        id=uuid4(),
        slug=slug,
        title=slug.title(),
        description="Test",
        is_public=is_public,
        created_at=datetime.now(timezone.utc),
    )
    if is_archived:
        sk.is_archived = True
    db.add(sk)
    db.flush()
    return sk


def _make_version_row(db, skill_id, semver: str = "1.0.0", skill_toml: str | None = None):
    from datetime import datetime, timezone
    from app.models import SkillVersion
    v = SkillVersion(
        id=uuid4(),
        skill_id=skill_id,
        semver=semver,
        tarball_size_bytes=100,
        checksum_sha256="ab" * 32,
        skill_toml=skill_toml,
        created_at=datetime.now(timezone.utc),
    )
    db.add(v)
    db.flush()
    return v


_SANDBOX_TOML = "[sandbox]\nnetwork_allow = []\nmemory_mb = 256\ntimeout_seconds = 60\n"
_MASTER_CTX = AuthContext(scope="master")


class TestSandboxRoutesCoverage:

    def _client(self, db, auth_ctx=None):
        from app.database import get_db as _get_db
        if auth_ctx is None:
            auth_ctx = _MASTER_CTX
        app = _make_sandbox_app(auth_ctx)
        app.dependency_overrides[_get_db] = lambda: db
        return TestClient(app, raise_server_exceptions=False)

    def test_status_skill_not_found_404(self, sandbox_db):
        """GET /status for unknown skill → 404 (line 80)."""
        client = self._client(sandbox_db)
        resp = client.get("/api/skills/ghost-skill/sandbox/status")
        assert resp.status_code == 404

    def test_status_private_skill_non_master_404(self, sandbox_db):
        """Private skill + non-master scope → 404 (line 88)."""
        sk = _make_skill_row(sandbox_db, "private-sandbox-skill", is_public=False)
        _make_version_row(sandbox_db, sk.id, skill_toml=_SANDBOX_TOML)
        ctx = AuthContext(scope="user", user_id=uuid4())
        client = self._client(sandbox_db, auth_ctx=ctx)
        resp = client.get(f"/api/skills/{sk.slug}/sandbox/status")
        assert resp.status_code == 404

    def test_status_no_toml_returns_not_supported(self, sandbox_db):
        """Skill with no skill_toml → sandbox_supported=False (lines 92-97)."""
        sk = _make_skill_row(sandbox_db, "no-toml-skill")
        _make_version_row(sandbox_db, sk.id, skill_toml=None)
        client = self._client(sandbox_db)
        resp = client.get(f"/api/skills/{sk.slug}/sandbox/status")
        assert resp.status_code == 200
        assert resp.json()["sandbox_supported"] is False

    def test_status_invalid_toml_returns_not_supported(self, sandbox_db):
        """Invalid skill_toml → sandbox_supported=False (lines 100-106)."""
        sk = _make_skill_row(sandbox_db, "invalid-toml-skill")
        _make_version_row(sandbox_db, sk.id, skill_toml="this {{{ is not toml")
        client = self._client(sandbox_db)
        resp = client.get(f"/api/skills/{sk.slug}/sandbox/status")
        assert resp.status_code == 200
        assert resp.json()["sandbox_supported"] is False

    def test_status_success_with_sandbox_block(self, sandbox_db):
        """Valid toml with [sandbox] block → sandbox_supported=True (lines 108-123)."""
        sk = _make_skill_row(sandbox_db, "valid-sandbox-status-skill")
        _make_version_row(sandbox_db, sk.id, skill_toml=_SANDBOX_TOML)
        client = self._client(sandbox_db)
        resp = client.get(f"/api/skills/{sk.slug}/sandbox/status")
        assert resp.status_code == 200
        assert resp.json()["sandbox_supported"] is True

    def test_run_skill_not_found_404(self, sandbox_db):
        """POST /run for unknown skill → 404 (line 150)."""
        client = self._client(sandbox_db)
        resp = client.post("/api/skills/ghost-run-skill/sandbox/run", json={})
        assert resp.status_code == 404

    def test_run_private_archived_skill_non_master_404(self, sandbox_db):
        """Private+archived skill + non-master → 404 (lines 155-157)."""
        sk = _make_skill_row(sandbox_db, "archived-run-skill", is_public=False, is_archived=True)
        _make_version_row(sandbox_db, sk.id, skill_toml=_SANDBOX_TOML)
        ctx = AuthContext(scope="user", user_id=uuid4(), is_sandbox_operator=True)
        client = self._client(sandbox_db, auth_ctx=ctx)
        resp = client.post(f"/api/skills/{sk.slug}/sandbox/run", json={})
        assert resp.status_code == 404

    def test_run_specific_version_not_found(self, sandbox_db):
        """?version pinned but not found → 404 (line 163)."""
        sk = _make_skill_row(sandbox_db, "ver-missing-skill")
        _make_version_row(sandbox_db, sk.id, "1.0.0", skill_toml=_SANDBOX_TOML)
        client = self._client(sandbox_db)
        resp = client.post(f"/api/skills/{sk.slug}/sandbox/run", json={"version": "9.9.9"})
        assert resp.status_code == 404

    def test_run_no_toml_returns_400(self, sandbox_db):
        """Skill with version but no toml → 400 (lines 171-175)."""
        sk = _make_skill_row(sandbox_db, "no-toml-run-skill")
        _make_version_row(sandbox_db, sk.id, skill_toml=None)
        client = self._client(sandbox_db)
        resp = client.post(f"/api/skills/{sk.slug}/sandbox/run", json={})
        assert resp.status_code == 400

    def test_run_no_versions_404(self, sandbox_db):
        """Skill with no versions → 404 (lines 167-168)."""
        sk = _make_skill_row(sandbox_db, "no-ver-run-skill")
        client = self._client(sandbox_db)
        resp = client.post(f"/api/skills/{sk.slug}/sandbox/run", json={})
        assert resp.status_code == 404

    def test_run_no_sandbox_block_returns_400(self, sandbox_db):
        """Toml without [sandbox] block → 400 (lines 183-187)."""
        toml_no_sandbox = "[meta]\nslug = \"x\"\n"
        sk = _make_skill_row(sandbox_db, "no-sandbox-block-skill")
        _make_version_row(sandbox_db, sk.id, skill_toml=toml_no_sandbox)
        client = self._client(sandbox_db)
        resp = client.post(f"/api/skills/{sk.slug}/sandbox/run", json={})
        assert resp.status_code == 400

    def test_run_no_skill_dir_returns_500(self, sandbox_db, tmp_path):
        """No skill checkout directory found → 500 (lines 196-200)."""
        sk = _make_skill_row(sandbox_db, "no-dir-skill")
        _make_version_row(sandbox_db, sk.id, skill_toml=_SANDBOX_TOML)
        client = self._client(sandbox_db)
        with patch("app.sandbox.routes._resolve_skill_dir", return_value=None):
            resp = client.post(f"/api/skills/{sk.slug}/sandbox/run", json={})
        assert resp.status_code == 500

    def test_run_with_specific_version_pinned(self, sandbox_db, tmp_path):
        """Run with existing ?version= finds the right version (lines 160-163)."""
        skill_dir = str(tmp_path / "skill")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "setup.sh"), "w") as f:
            f.write("#!/bin/bash\necho ok\n")

        sk = _make_skill_row(sandbox_db, "pinned-version-run-skill")
        v1 = _make_version_row(sandbox_db, sk.id, "1.0.0", skill_toml=_SANDBOX_TOML)
        _make_version_row(sandbox_db, sk.id, "2.0.0", skill_toml=_SANDBOX_TOML)

        mock_result = MagicMock()
        mock_result.sandbox_id = "abc123"
        mock_result.exit_code = 0
        mock_result.stdout = "ok"
        mock_result.stderr = ""
        mock_result.timed_out = False
        mock_result.duration_seconds = 0.1
        mock_result.success = True
        mock_result.error = None

        client = self._client(sandbox_db)
        with patch("app.sandbox.routes._resolve_skill_dir", return_value=skill_dir):
            with patch("app.sandbox.routes.get_runner") as mock_get_runner:
                mock_runner = MagicMock()
                mock_runner.run.return_value = mock_result
                mock_get_runner.return_value = mock_runner
                resp = client.post(f"/api/skills/{sk.slug}/sandbox/run", json={"version": "1.0.0"})
        assert resp.status_code == 200
