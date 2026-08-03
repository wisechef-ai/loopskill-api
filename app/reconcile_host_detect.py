"""Host agent auto-detection + one-command install — evergreen_0206 Phase D.

Decision #15 (EASY): `recipes daemon install` auto-detects the host agent and
wires the reconcile loop with zero hand-config. Per Adam (q2, 2026-06-03): ship
Hermes + Codex detection live (both are real dogfood hosts — Chef & Varys =
Hermes, Codex = second validator); Claude / OpenCode are a thin follow-on.

Detection is by skills-directory convention:
  Hermes  → ~/.hermes/skills/
  Codex   → ~/.codex/skills/   (Codex CLI skill dir)
  Claude  → ~/.claude/skills/   (detected, follow-on wiring)
  OpenCode→ ~/.opencode/skills/ (detected, follow-on wiring)

The detector returns the host kind + its skills dir + whether reconcile wiring
is live for that host. It NEVER guesses a single host when several are present —
it returns all detected, and the installer picks per an explicit --host flag or
the highest-priority live host.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Host kinds with live reconcile wiring this sprint (Adam q2).
LIVE_HOSTS = frozenset({"hermes", "codex"})

# Detection table: kind → skills-dir suffix under $HOME.
_HOST_SKILLS_DIRS: dict[str, str] = {
    "hermes": ".hermes/skills",
    "codex": ".codex/skills",
    "claude": ".claude/skills",
    "opencode": ".opencode/skills",
}

# Priority when multiple hosts are present and no --host is given.
_HOST_PRIORITY = ["hermes", "codex", "claude", "opencode"]


@dataclass(frozen=True)
class DetectedHost:
    kind: str
    skills_dir: Path
    live: bool  # True → reconcile wiring is shipped for this host kind


def detect_hosts(home: Path | None = None) -> list[DetectedHost]:
    """Return every agent host whose skills dir exists under *home*.

    home defaults to the real $HOME; injectable for tests.
    """
    base = Path(home) if home is not None else Path.home()
    found: list[DetectedHost] = []
    for kind in _HOST_PRIORITY:
        skills_dir = base / _HOST_SKILLS_DIRS[kind]
        if skills_dir.is_dir():
            found.append(DetectedHost(kind=kind, skills_dir=skills_dir, live=kind in LIVE_HOSTS))
    return found


def select_host(home: Path | None = None, prefer: str | None = None) -> DetectedHost | None:
    """Pick the host to install onto.

    prefer (an explicit --host) wins if present and detected. Otherwise the
    highest-priority LIVE host is chosen. Returns None when nothing usable is
    detected.
    """
    hosts = detect_hosts(home)
    if not hosts:
        return None

    if prefer:
        for h in hosts:
            if h.kind == prefer:
                return h
        # Explicit preference not detected → caller should error, not silently
        # fall back to a different host's skills dir.
        return None

    # No preference: first LIVE host by priority.
    for h in hosts:
        if h.live:
            return h
    # Detected hosts exist but none are live-wired yet.
    return hosts[0]


def cron_template(host: DetectedHost, cookbook_id: str, api_base: str) -> str:
    """Render a host-appropriate reconcile cron line / unit.

    For Hermes: a cron prompt template the host scheduler runs.
    For Codex: a shell line suitable for the host's cron/launchd.

    The intelligence is server-side; this is just the trigger that pulls a diff
    and applies it atomically via the reconcile client.
    """
    lockfile = host.skills_dir.parent / "recipes-lock.json"
    if host.kind == "hermes":
        return (
            f"# recipes reconcile (evergreen_0206) — Hermes host\n"
            f"# every 30m: pull diff for cookbook {cookbook_id}, atomic-apply\n"
            f"*/30 * * * * recipes-reconcile "
            f"--cookbook {cookbook_id} --api {api_base} "
            f"--skills-dir {host.skills_dir} --lockfile {lockfile}\n"
        )
    # codex (and future hosts) — generic shell cron line.
    return (
        f"# recipes reconcile (evergreen_0206) — {host.kind} host\n"
        f"*/30 * * * * recipes-reconcile "
        f"--cookbook {cookbook_id} --api {api_base} "
        f"--skills-dir {host.skills_dir} --lockfile {lockfile}\n"
    )


# ── converge_0208 P4 — the LOOP half of the same trigger ─────────────────────

# The literal an installer greps to replace its own line instead of appending a
# second one. Rendering and idempotency must agree on one token, so it lives
# here next to the renderer rather than being retyped in shell.
LOOP_APPLY_CRON_MARKER = "app.loop_apply_cli"

# app/loop_apply.py speaks exactly one scheduler dialect: the Hermes
# ~/.hermes/cron/jobs.json document. Codex/Claude/OpenCode are detected by
# detect_hosts() for the SKILL path and are not wired for the loop path.
LOOP_APPLY_HOSTS = frozenset({"hermes"})


def loop_apply_cron_template(
    host: DetectedHost,
    api_base: str,
    *,
    jobs_file: Path | None = None,
    python_bin: str = "python3",
    interval_minutes: int = 30,
    key_file: Path | None = None,
    pythonpath: Path | None = None,
) -> str:
    """Render the cron line that keeps a member's loop crons reconciled.

    The loop-path sibling of :func:`cron_template`. ``app.loop_apply_cli`` pulls
    ``GET /api/my/loop-assignments`` with the MEMBER key and reconciles the
    ``loopskill/``-namespaced jobs in the host scheduler to what it finds —
    create on assign, update on change, remove on undeploy.

    ``key_file`` renders the member key as a ``$(cat …)`` read rather than an
    inlined literal: a crontab is readable by every process its owner runs, and
    an inlined key shows up in ``ps`` on every fire. The reconcile-path
    installer inlines its key; that is a wart, not a precedent to copy.

    Raises ValueError for any host kind loop-apply cannot actually serve. A
    rendered-but-inert cron is the failure mode this sprint exists to stop:
    it looks installed, reports nothing, and converges nothing.
    """
    if host.kind not in LOOP_APPLY_HOSTS:
        raise ValueError(
            f"loop-apply is not wired for host kind {host.kind!r}: "
            f"app/loop_apply.py writes the Hermes cron jobs.json format, and "
            f"only hermes hosts have it. Supported: {sorted(LOOP_APPLY_HOSTS)}"
        )
    jobs = jobs_file if jobs_file is not None else host.skills_dir.parent / "cron" / "jobs.json"
    env = ""
    if key_file is not None:
        env += f'RECIPES_API_KEY="$(cat {key_file})" '
    if pythonpath is not None:
        env += f"PYTHONPATH={pythonpath} "
    return (
        f"# loopskill loop-apply (converge_0208 P4) — {host.kind} host\n"
        f"# every {interval_minutes}m: pull this member's loop assignments and\n"
        f"# reconcile the loopskill/-namespaced crons. Needs RECIPES_API_KEY set\n"
        f"# to the MEMBER key — loop assignments are a member surface.\n"
        f"*/{interval_minutes} * * * * {env}{python_bin} -m {LOOP_APPLY_CRON_MARKER} "
        f"--api {api_base} --jobs-file {jobs}\n"
    )


COLLECT_REPORTS_CRON_MARKER = "loopskill-collect-reports.py"


def collect_reports_cron_template(
    host: DetectedHost,
    script_path: Path,
    *,
    python_bin: str = "python3",
    interval_minutes: int = 30,
    key_file: Path | None = None,
) -> str:
    """Render the cron line that DRAINS the loop-run spool to the server.

    Without this the loop path stops one hop short of the server: a fired loop
    calls ``loopskill-emit-run.sh``, the record lands in
    ``~/.hermes/loopskill/outbox/`` — and stays there. The collector is the only
    thing that turns a spool file into a ``LoopRun`` row.

    ``key_file`` is passed by path, not by value, for the same reason as in
    :func:`loop_apply_cron_template`. The collector also reads that path on its
    own as a fallback, so the cron works either way — this makes the dependency
    visible in ``crontab -l`` instead of implicit in the script's defaults.
    """
    env = f"LOOPSKILL_MEMBER_KEY_FILE={key_file} " if key_file is not None else ""
    return (
        f"# loopskill sync-report collector (activate_0701 Phase T) — {host.kind} host\n"
        f"# every {interval_minutes}m: batch the loop-run spool + cron health into\n"
        f"# ONE POST /api/sync-report. Exits 0 on network failure; retries next cycle.\n"
        f"*/{interval_minutes} * * * * {env}{python_bin} {script_path}\n"
    )
