"""Pure, stdlib-only helpers for the loop runner (split from app.loop_runner).

Extracted to keep app/loop_runner.py under the 600-line god-object gate (W0.2).
Everything here is I/O-light and unit-testable in isolation: env scrubbing, path
safety, integer clamping, the bounded pipe reader, the rlimit preexec factory,
and the process-group kill. No FastAPI/DB/sandbox imports — safe to reuse from a
route, an MCP tool, or a future runner.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess  # noqa: S404 - controlled bounded execution of an author-provided verification script
import time

# Reserved on-disk name for the staged verification script; workspace_files may
# not collide with it. Kept here so _safe_workspace_path is self-contained.
VERIFY_SCRIPT_NAME = "__loop_verify.sh"

# Hard byte ceiling read from a child's pipe. A malicious verification_script can
# flood stdout (`yes | head -c 1G`); RLIMIT_AS caps the CHILD's memory but NOT the
# parent's, so an unbounded read would buffer the flood into the SERVER's memory
# and OOM it. We stop reading (and the caller kills the group) past this many
# bytes. Generous vs. the response truncation, tiny vs. an OOM.
MAX_CAPTURE_BYTES = 1_000_000  # 1 MB per stream


class WorkspaceError(ValueError):
    """Raised when caller-supplied workspace input violates a safety bound."""


def clamp_int(value: int | None, default: int, lo: int, hi: int) -> int:
    """Clamp ``value`` into [lo, hi], falling back to ``default`` when None/invalid."""
    if value is None:
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def safe_workspace_path(rel_path: str) -> str | None:
    """Return a normalised relative path, or None if it escapes the workspace.

    Rejects absolute paths, ``..`` traversal, null bytes, the reserved verify-
    script name, and anything that normalises outside the workspace root.
    """
    if not isinstance(rel_path, str) or not rel_path.strip():
        return None
    if "\x00" in rel_path:
        # Null bytes can truncate paths at the OS layer; reject outright.
        return None
    if rel_path.startswith("/") or rel_path.startswith("~"):
        return None
    norm = os.path.normpath(rel_path)
    if norm.startswith("..") or norm.startswith("/") or os.path.isabs(norm):
        return None
    if os.pardir in norm.split(os.sep):
        return None
    if norm in (".", ""):
        # Normalises to the workspace dir itself — open(workdir,"w") would raise
        # IsADirectoryError (not a WorkspaceError) and 500 + leak the tempdir.
        return None
    if os.path.basename(norm) == VERIFY_SCRIPT_NAME:
        return None
    return norm


# Env vars that turn "run this shell script" into "load this attacker library /
# source this attacker file" — blocked even when a caller supplies them, because
# they let a structurally-innocent script carry a dangerous payload (review F2).
DANGEROUS_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "GCONV_PATH",
        "NLSPATH",
        "HOSTALIASES",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PERL5LIB",
        "RUBYLIB",
        "NODE_OPTIONS",
        "IFS",
        "CDPATH",
        "PS4",
    }
)
# Caller env keys must look like ordinary env identifiers; anything else is dropped.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def scrub_env(env: dict[str, str] | None, workdir: str) -> dict[str, str]:
    """Build the MINIMAL env for the script: a clean base + caller-supplied vars.

    The server's own environment (DB URL, Stripe keys, master key, …) is NEVER
    inherited — that is a core safety property of verify-mode regardless of the
    kernel sandbox. Only an explicit, string-typed ``env`` from the caller passes,
    and only when the key is a plain identifier NOT in DANGEROUS_ENV_KEYS (an
    allowlist-shaped filter so a caller can't smuggle a loader-hijack var like
    LD_AUDIT / BASH_ENV / GCONV_PATH past us — review F2).
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
        if not _ENV_KEY_RE.match(key):
            continue
        if key in DANGEROUS_ENV_KEYS:
            continue
        base[key] = val
    return base


def make_rlimit_preexec(timeout: int, memory_mb: int):
    """Return a preexec_fn that hardens the forked child before exec.

    Applies POSIX rlimits and marks the child non-dumpable (so its own
    ``/proc/<pid>/environ`` flips to root-only — defense in depth against a peer
    same-UID reader; review F1).

    Each step is best-effort: a platform that lacks one must not abort the run.
    Note: ``os.setsid()`` is NOT called here — the caller passes
    ``start_new_session=True`` to Popen, which already does it; calling it twice
    is redundant and Python 3.11+ documents the two as mutually exclusive (review
    F13). RLIMIT_NPROC is deliberately NOT set: it is per-UID, so on a shared
    server UID a fixed cap makes the child fail with "Cannot fork" the moment the
    UID already runs that many processes. Real fork-bomb containment needs the
    ``sandboxed`` backend or a dedicated UID/cgroup — which is exactly why
    WR_LOOP_RUN_REQUIRE_SANDBOX exists for multi-tenant deployers (review F6).
    """

    def _preexec() -> None:  # pragma: no cover - runs in the forked child
        import ctypes
        import resource

        # Mark the child non-dumpable: kernel reassigns its /proc/<pid>/* to
        # root:root 0400, so a same-UID peer cannot read this child's environ.
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
        except Exception:  # noqa: BLE001 - best-effort hardening; never abort the run
            pass

        # RLIMIT_CPU caps CPU time as a backstop to the wall-clock poll; RLIMIT_AS
        # caps memory; RLIMIT_FSIZE caps single-file size.
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


def read_bounded(proc: subprocess.Popen, timeout: int) -> tuple[bytes, bytes, bool, bool]:
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


def kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group (it was started in a new session)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
