"""Loop runner — execute a published loop's verification under enforced bounds.

loopskill_0622 Phase 8 follow-on (loopskill_run_0627). The loop *registry*
stores a safety-bounded contract for every loop; this module makes that contract
**executable** — the white-space wedge behind the 100k-star goal is not "a list of
loops" but "a registry that RUNS the loop's success guarantee and tells you,
objectively, pass or fail."

What "run" means in v1 — VERIFY-MODE
------------------------------------
A loop publishes a ``verification_script``: an OBJECTIVE check of its
``success_condition`` (exit 0 == success). Verify-mode executes that author-
provided, pre-visible script against a caller-supplied workspace, under the
loop's enforced bounds, and returns pass/fail. No LLM is involved — this layer is
deterministic, CI-testable, and ships in the open-core repo with zero extra deps.

The LLM agent-driving layer (drive ``system_prompt`` for <= ``max_turns``, calling
only ``tool_allowlist`` tools, stopping on ``stopping_criteria``/``budget_usd``)
is a deliberate roadmap item: it needs a heavy LLM client the OSS repo omits on
purpose. The :class:`LoopDriver` protocol below is the drop-in seam for it.

Tiered confinement (the load-bearing honesty)
---------------------------------------------
The cold-clone ``docker compose up`` image ships NO kernel sandbox (firejail/bwrap
are not installed, and the container runs non-root). A runner that hard-depends on
the kernel sandbox would error on the exact demo path that wins stars. So the
runner enforces the *strongest confinement available* and DECLARES the level it
achieved in the response:

  - ``sandboxed`` : firejail/bwrap present AND functional -> full kernel
                    confinement (network none/filtered, isolated fs, mem cap,
                    wall timeout). Production hosts opt into this by installing a
                    backend.
  - ``bounded``   : no working kernel sandbox -> POSIX backstops that need no
                    privileges: RLIMIT_AS (memory), RLIMIT_CPU, RLIMIT_FSIZE, a
                    hard wall-clock timeout (SIGKILL the process group), a fresh
                    isolated workspace, and a SCRUBBED env (only caller-supplied
                    vars pass — the server's own secrets never reach the script).

Both modes share the two properties that make verify-mode safe to expose to an
authenticated caller: the script is author-provided + validated on publish, and
the executing environment carries no inherited server credentials.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess  # noqa: S404 - controlled execution of an author-provided, bounded verification script
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.sandbox.profile import SandboxProfile
from app.sandbox.runner import SandboxRunner

logger = logging.getLogger(__name__)

# ── Run-level caps (independent of any single loop's declared bounds) ─────────
# A verification run is a short objective check, not the loop's full execution.
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 600  # mirrors SandboxProfile.validate ceiling
DEFAULT_MEMORY_MB = 512
MAX_OUTPUT_CHARS = 8000  # stdout/stderr truncation in the response
# Hard byte ceiling read from the child's pipes. A malicious verification_script
# can flood stdout (e.g. `yes | head -c 1G`); RLIMIT_AS caps the CHILD's memory
# but NOT the parent's, so an unbounded communicate() would buffer the flood into
# the SERVER's memory and OOM it. We stop reading (and kill the process group)
# once a stream exceeds this many bytes. Generous vs. MAX_OUTPUT_CHARS so genuine
# diagnostic output is never lost, tiny vs. an OOM.
MAX_CAPTURE_BYTES = 1_000_000  # 1 MB per stream
MAX_WORKSPACE_FILES = 64
MAX_WORKSPACE_FILE_BYTES = 256 * 1024  # 256 KB per file
VERIFY_SCRIPT_NAME = "__loop_verify.sh"

# Where bounded-mode workspaces are staged. Overridable for tests / ops.
RUN_WORKSPACE_BASE = os.environ.get("WR_LOOP_RUN_WORKSPACE", tempfile.gettempdir())


@dataclass
class LoopRunResult:
    """Outcome of a single loop verification run.

    ``passed`` is the objective verdict: the verification_script exited 0 and did
    not time out and the runner hit no internal error.
    """

    run_id: str
    mode: str  # "verify"
    confinement: str  # "sandboxed" | "bounded"
    passed: bool
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    bounds: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise, truncating captured output to MAX_OUTPUT_CHARS."""
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "confinement": self.confinement,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "stdout": self.stdout[:MAX_OUTPUT_CHARS],
            "stderr": self.stderr[:MAX_OUTPUT_CHARS],
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration_seconds, 3),
            "bounds": self.bounds,
            "error": self.error,
        }


@runtime_checkable
class LoopDriver(Protocol):
    """Seam for the future LLM agent-driving layer (roadmap, not v1).

    A driver runs the loop's ``system_prompt`` for up to ``max_turns``, calling
    only ``tool_allowlist`` tools, stopping on ``stopping_criteria`` / budget, and
    finally invoking the verification_script. The OSS repo deliberately ships no
    LLM client, so no concrete driver is bundled — this Protocol documents the
    contract a BYO-LLM driver implements to plug into ``POST /api/loops/{slug}/run``
    with ``mode="agent"``.
    """

    def drive(self, loop: Any, run_input: dict[str, Any]) -> LoopRunResult:  # pragma: no cover - interface
        """Drive the loop autonomously, then verify. Returns the run result."""
        ...


class LoopRunner:
    """Execute a loop's verification under the strongest confinement available."""

    def __init__(self, workspace_base: str | None = None) -> None:
        self._workspace_base = workspace_base or RUN_WORKSPACE_BASE
        # Lazily resolve the kernel-sandbox backend. SandboxRunner._detect_backend
        # raises on macOS (no firejail/bwrap); treat that as "no backend" so the
        # runner still works in bounded mode there.
        try:
            detected = SandboxRunner._detect_backend()  # noqa: SLF001 - intentional reuse of the detector
        # Rationale: backend detection failure (macOS / missing binaries) -> bounded mode, never crash the runner.
        except Exception:  # noqa: BLE001
            detected = "none"
        # A backend that is *installed* but cannot actually confine on this host
        # (e.g. bwrap's --unshare-net fails with RTM_NEWADDR EPERM inside a
        # restricted container / hardened VM) must not silently fail every run.
        # Probe it once with a trivial canary; demote to bounded mode on failure
        # so the runner is genuinely run-everywhere (the cold-clone demo path).
        if detected in ("firejail", "bwrap") and not self._backend_functional(detected):
            logger.warning(
                "sandbox backend %r is installed but non-functional on this host; "
                "falling back to bounded (POSIX rlimit) confinement",
                detected,
            )
            detected = "none"
        self._backend = detected

    def _backend_functional(self, backend: str) -> bool:
        """Return True iff ``backend`` can actually run a no-op under confinement.

        Cheap one-shot canary. We don't trust mere presence on PATH because some
        environments install the binary but deny the namespaces it needs.
        """
        probe_dir = None
        try:
            os.makedirs(self._workspace_base, exist_ok=True)
            probe_dir = tempfile.mkdtemp(prefix="loop-probe-", dir=self._workspace_base)
            with open(os.path.join(probe_dir, VERIFY_SCRIPT_NAME), "w", encoding="utf-8") as fh:
                fh.write("exit 0\n")
            profile = SandboxProfile(
                network_allow=[],
                fs_write=["/tmp"],
                exec_allow=["sh"],
                memory_mb=128,
                timeout_seconds=10,
                env_pass=[],
            )
            runner = SandboxRunner(workspace=self._workspace_base)
            runner._backend = backend  # noqa: SLF001 - pin the backend under probe
            sb = runner.run(
                skill_dir=probe_dir,
                entrypoint=VERIFY_SCRIPT_NAME,
                profile=profile,
                skill_slug="backend-probe",
            )
            return sb.exit_code == 0 and sb.error is None and not sb.timed_out
        # Rationale: any probe failure -> treat backend as non-functional, fall back to bounded.
        except Exception:  # noqa: BLE001
            return False
        finally:
            if probe_dir:
                shutil.rmtree(probe_dir, ignore_errors=True)

    @property
    def backend(self) -> str:
        return self._backend

    # ── public API ────────────────────────────────────────────────────────────

    def run_verification(
        self,
        *,
        loop_slug: str,
        verification_script: str,
        declared_bounds: dict[str, Any],
        workspace_files: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        memory_mb: int | None = None,
        allow_network: bool = False,
    ) -> LoopRunResult:
        """Run ``verification_script`` for ``loop_slug`` and return the verdict.

        The script runs in a fresh isolated workspace, with ONLY ``env`` exposed
        (no inherited server environment), bounded by ``timeout_seconds`` and
        ``memory_mb``. ``declared_bounds`` (the loop's max_turns/budget/allowlist)
        are echoed back in the result so the caller sees the envelope.
        """
        run_id = uuid.uuid4().hex[:12]
        start = time.monotonic()

        timeout = _clamp_int(timeout_seconds, DEFAULT_TIMEOUT_SECONDS, 1, MAX_TIMEOUT_SECONDS)
        memory = _clamp_int(memory_mb, DEFAULT_MEMORY_MB, 64, 4096)

        bounds = dict(declared_bounds)
        bounds.update(
            run_timeout_seconds=timeout,
            run_memory_mb=memory,
            network=bool(allow_network),
        )

        if not (verification_script or "").strip():
            # By the publish contract this cannot happen, but fail explicit-closed.
            return LoopRunResult(
                run_id=run_id,
                mode="verify",
                confinement="bounded",
                passed=False,
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                duration_seconds=time.monotonic() - start,
                bounds=bounds,
                error="loop has no verification_script (cannot verify)",
            )

        # Stage an isolated workspace containing the verify script + caller files.
        try:
            workdir = self._stage_workspace(run_id, verification_script, workspace_files)
        except _WorkspaceError as exc:
            return LoopRunResult(
                run_id=run_id,
                mode="verify",
                confinement="bounded",
                passed=False,
                exit_code=-1,
                stdout="",
                stderr="",
                timed_out=False,
                duration_seconds=time.monotonic() - start,
                bounds=bounds,
                error=str(exc),
            )

        clean_env = _scrub_env(env, workdir)

        try:
            if self._backend in ("firejail", "bwrap"):
                result = self._run_sandboxed(
                    run_id, workdir, clean_env, timeout, memory, allow_network, start
                )
                result.confinement = "sandboxed"
            else:
                result = self._run_bounded(run_id, workdir, clean_env, timeout, memory, start)
                result.confinement = "bounded"
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

        result.bounds = bounds
        return result

    # ── workspace staging ──────────────────────────────────────────────────────

    def _stage_workspace(
        self, run_id: str, verification_script: str, workspace_files: dict[str, str] | None
    ) -> str:
        os.makedirs(self._workspace_base, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix=f"loop-run-{run_id}-", dir=self._workspace_base)

        # The verification script itself.
        script_path = os.path.join(workdir, VERIFY_SCRIPT_NAME)
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(verification_script)

        files = workspace_files or {}
        if len(files) > MAX_WORKSPACE_FILES:
            raise _WorkspaceError(f"too many workspace_files (max {MAX_WORKSPACE_FILES})")

        for rel_path, content in files.items():
            safe_rel = _safe_workspace_path(rel_path)
            if safe_rel is None:
                raise _WorkspaceError(f"unsafe workspace file path: {rel_path!r}")
            if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_WORKSPACE_FILE_BYTES:
                raise _WorkspaceError(f"workspace file too large or non-string: {rel_path!r}")
            dest = os.path.join(workdir, safe_rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(content)

        return workdir

    # ── sandboxed backend (reuse the kernel sandbox) ────────────────────────────

    def _run_sandboxed(
        self,
        run_id: str,
        workdir: str,
        env: dict[str, str],
        timeout: int,
        memory: int,
        allow_network: bool,
        start: float,
    ) -> LoopRunResult:
        # env_pass=[] so SandboxProfile injects NOTHING from the server os.environ;
        # only the explicit ``env`` dict (passed below) reaches the script.
        profile = SandboxProfile(
            network_allow=["*"] if allow_network else [],
            fs_write=["/tmp"],
            exec_allow=["sh", "bash", "python3", "jq", "test", "wc", "awk", "grep", "cat"],
            memory_mb=memory,
            timeout_seconds=timeout,
            env_pass=[],
        )
        runner = SandboxRunner(workspace=self._workspace_base)
        sb = runner.run(
            skill_dir=workdir,
            entrypoint=VERIFY_SCRIPT_NAME,
            profile=profile,
            skill_slug=run_id,
            env=env,
        )
        duration = time.monotonic() - start
        passed = sb.exit_code == 0 and not sb.timed_out and sb.error is None
        return LoopRunResult(
            run_id=run_id,
            mode="verify",
            confinement="sandboxed",
            passed=passed,
            exit_code=sb.exit_code,
            stdout=sb.stdout,
            stderr=sb.stderr,
            timed_out=sb.timed_out,
            duration_seconds=duration,
            error=sb.error,
        )

    # ── bounded backend (POSIX rlimits, no privileges needed) ───────────────────

    def _run_bounded(
        self,
        run_id: str,
        workdir: str,
        env: dict[str, str],
        timeout: int,
        memory: int,
        start: float,
    ) -> LoopRunResult:
        script_path = os.path.join(workdir, VERIFY_SCRIPT_NAME)
        preexec = _make_rlimit_preexec(timeout, memory)

        try:
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, author script, bounded + scrubbed env
                ["sh", script_path],
                cwd=workdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=preexec,  # noqa: PLW1509 - rlimits must be set in the child pre-exec
                start_new_session=True,
            )
        # Rationale: spawn failure (no sh, OSError) -> fail-closed error result, never raise into the request.
        except Exception as exc:  # noqa: BLE001
            return LoopRunResult(
                run_id=run_id,
                mode="verify",
                confinement="bounded",
                passed=False,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                timed_out=False,
                duration_seconds=time.monotonic() - start,
                error=f"spawn failed: {exc}",
            )

        timed_out = False
        # Bounded read: cap each stream at MAX_CAPTURE_BYTES so a flooding script
        # can never buffer unbounded output into the SERVER's memory. Overflow or
        # timeout kills the whole process group.
        stdout_b, stderr_b, overflowed, timed_out = _read_bounded(proc, timeout)
        if timed_out or overflowed:
            _kill_process_group(proc)
            try:
                proc.wait(timeout=5)
            # Rationale: child may already be reaped or wedged; never block the request on wait.
            except Exception:  # noqa: BLE001
                pass

        duration = time.monotonic() - start
        exit_code = proc.returncode if proc.returncode is not None else -1
        passed = exit_code == 0 and not timed_out and not overflowed
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if overflowed:
            stderr = (stderr + "\n[loopskill] output exceeded capture limit; run aborted.").strip()
        return LoopRunResult(
            run_id=run_id,
            mode="verify",
            confinement="bounded",
            passed=passed,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_seconds=duration,
            error=("timed out" if timed_out else ("output limit exceeded" if overflowed else None)),
        )


# ── module-level helpers (pure, unit-testable) ────────────────────────────────


class _WorkspaceError(ValueError):
    """Raised when caller-supplied workspace input violates a safety bound."""


def _clamp_int(value: int | None, default: int, lo: int, hi: int) -> int:
    """Clamp ``value`` into [lo, hi], falling back to ``default`` when None/invalid."""
    if value is None:
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _safe_workspace_path(rel_path: str) -> str | None:
    """Return a normalised relative path, or None if it escapes the workspace.

    Rejects absolute paths, ``..`` traversal, the reserved verify-script name, and
    anything that normalises outside the workspace root.
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        return None
    if rel_path.startswith("/") or rel_path.startswith("~"):
        return None
    norm = os.path.normpath(rel_path)
    if norm.startswith("..") or norm.startswith("/") or os.path.isabs(norm):
        return None
    if os.pardir in norm.split(os.sep):
        return None
    if os.path.basename(norm) == VERIFY_SCRIPT_NAME:
        return None
    return norm


def _scrub_env(env: dict[str, str] | None, workdir: str) -> dict[str, str]:
    """Build the MINIMAL env for the script: a clean base + caller-supplied vars.

    The server's own environment (DB URL, Stripe keys, master key, …) is NEVER
    inherited — that is the core safety property of verify-mode regardless of the
    kernel sandbox. Only an explicit, string-typed ``env`` from the caller passes.
    """
    base = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": workdir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "SANDBOX": "1",
        "LOOPSKILL_VERIFY": "1",
    }
    for key, val in (env or {}).items():
        if not isinstance(key, str) or not isinstance(val, str):
            continue
        # Never let the caller override PATH/HOME to point at host resources.
        if key in ("PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH"):
            continue
        base[key] = val
    return base


def _make_rlimit_preexec(timeout: int, memory_mb: int):
    """Return a preexec_fn that applies POSIX rlimits + a new session in the child.

    Each limit is best-effort: a platform that lacks one must not abort the run.
    RLIMIT_NPROC is intentionally NOT set — it is per-UID, so a low value would
    throttle the shared server process. Fork-bomb containment is a property of the
    ``sandboxed`` backend; ``bounded`` mode declares its weaker envelope honestly.
    """

    def _preexec() -> None:  # pragma: no cover - runs in the forked child
        import resource

        try:
            os.setsid()
        except OSError:
            pass
        # Wall-clock is enforced by communicate(timeout); RLIMIT_CPU caps CPU time
        # as a second backstop against a busy-loop that ignores SIGTERM.
        for res_name, soft, hard in (
            ("RLIMIT_CPU", timeout, timeout + 2),
            ("RLIMIT_AS", memory_mb * 1024 * 1024, memory_mb * 1024 * 1024),
            ("RLIMIT_FSIZE", 50 * 1024 * 1024, 50 * 1024 * 1024),
        ):
            res = getattr(resource, res_name, None)
            if res is None:
                continue
            try:
                resource.setrlimit(res, (soft, hard))
            except (ValueError, OSError):
                continue

    return _preexec


def _read_bounded(proc: subprocess.Popen, timeout: int) -> tuple[bytes, bytes, bool, bool]:
    """Read proc's stdout+stderr with a per-stream byte cap and a wall timeout.

    Returns (stdout_bytes, stderr_bytes, overflowed, timed_out). Reads each pipe
    in its own thread, stopping at MAX_CAPTURE_BYTES so a flooding child can never
    buffer unbounded data into the parent (server) memory. The caller kills the
    process group when overflowed or timed_out is True.
    """
    import threading

    chunks: dict[str, list[bytes]] = {"out": [], "err": []}
    totals: dict[str, int] = {"out": 0, "err": 0}
    overflow = threading.Event()

    def _pump(stream, key: str) -> None:
        if stream is None:
            return
        try:
            while True:
                buf = stream.read(65536)
                if not buf:
                    break
                room = MAX_CAPTURE_BYTES - totals[key]
                if room > 0:
                    chunks[key].append(buf[:room])
                    totals[key] += min(len(buf), room)
                if totals[key] >= MAX_CAPTURE_BYTES:
                    overflow.set()
                    break
        # Rationale: pipe read can fail if the child is killed mid-read; stop pumping.
        except Exception:  # noqa: BLE001
            pass

    t_out = threading.Thread(target=_pump, args=(proc.stdout, "out"), daemon=True)
    t_err = threading.Thread(target=_pump, args=(proc.stderr, "err"), daemon=True)
    t_out.start()
    t_err.start()

    # Poll for exit OR overflow so a flooding child is killed promptly (not only
    # at the wall timeout). proc.wait() alone wouldn't observe the overflow event.
    timed_out = False
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            break  # child exited on its own
        if overflow.is_set():
            break  # cap hit — caller kills the group
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.02)

    # Give the pump threads a brief window to drain buffered output post-exit.
    t_out.join(timeout=2)
    t_err.join(timeout=2)
    return b"".join(chunks["out"]), b"".join(chunks["err"]), overflow.is_set(), timed_out


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group (it was started in a new session)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


# Singleton, mirroring app.sandbox.routes.get_runner().
_runner: LoopRunner | None = None


def get_loop_runner() -> LoopRunner:
    """Return the process-wide LoopRunner singleton."""
    global _runner
    if _runner is None:
        _runner = LoopRunner()
    return _runner
