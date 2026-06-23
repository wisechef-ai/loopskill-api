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
