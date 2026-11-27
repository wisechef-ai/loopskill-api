#!/usr/bin/env python3
"""install_count_drift_probe — hourly drift monitor for skill install counts.

Compares Skill.install_count (denormalised counter) to the recomputed truth
(union of telemetry installs + install_events) and POSTs to
/api/v1/skill-error with quality:hardcoded-path label when drift > 0.

Run hourly via the `wisechef` user crontab on wisechef-hq — the schedule of
record is ``CRONTAB_LINE`` below, mirrored byte-for-byte in
``deploy/crontab.install-count-drift-probe``. Idempotent — re-running with no
drift is a no-op.

Sentinel file `/var/lib/recipes-api/last_backfill_at` is updated on every
successful run with the current ISO timestamp; the transparency endpoint
reads it (``app.transparency_routes.BACKFILL_SENTINEL_PATH`` — same file) and
flags it stale once it is older than 24h.

### Issue-flood gate (t_a1edaf10)

``report_drift`` POSTs one /api/v1/skill-error per drifting slug. Prod carries
~68 legacy drifting rows (telemetry-only counters, PR #493), so the re-homed
cron must not turn them into 68 issues. Three layers, all driven by a durable
ledger of KNOWN drift (``KNOWN_DRIFT_LEDGER``, ``{slug: (actual, expected)}``):

  1. BASELINE — the first run (ledger absent/corrupt) records every current
     drift as known and reports NOTHING. The sentinel is still written, so the
     freshness signal starts immediately.
  2. DEDUPE   — afterwards only a slug that is new, or whose (actual, expected)
     changed, is a candidate; a known one is never re-reported. A slug that stops
     drifting leaves the ledger, so it can be reported again if it re-drifts.
  3. CAP      — at most ``MAX_REPORTS_PER_RUN`` POSTs per run; the rest are
     deferred (left out of the ledger) and picked up by later runs.

It fails CLOSED: if the ledger cannot be persisted the run reports nothing,
because every hourly run would otherwise look like a first run.

### Signature stability (repohygiene_2605 Phase A)

Pre-fix: `error_signature` was derived from Python's builtin ``hash()``, which
is salt-randomized per process (PYTHONHASHSEED defaults to "random"). Every
hourly cron invocation produced a brand-new signature for the same
``(slug, drift_kind)`` tuple. The dispatcher had no dedup match and opened a
fresh GitHub issue each hour — 127 noise issues accumulated in ~5 days.

Post-fix:
  * ``compute_signature(slug)`` uses ``hashlib.sha256`` over a canonical input
    so the signature is stable across processes, hosts, clocks, and users.
  * ``limiter_signature(slug, actual, expected)`` is the rate-limiter key: it
    includes the drift values, so a changed value is not delayed by an earlier
    report of the same slug. (``compute_signature`` stays value-blind: it is the
    wire ``error_signature`` the tracker dedupes on.)
  * ``should_report(slug, sig, state_path, now_ts)`` enforces a per-skill,
    per-signature 24h rate limit backed by a tiny JSON state file. Even if
    upstream dedup ever regresses again, the probe itself won't re-spam.

Both helpers are PURE FUNCTIONS — testable without DB, network, or filesystem
side-effects (rate limit takes ``state_path`` + ``now_ts`` as injectable args).

Exit codes:
    0  no drift OR drift reported successfully
    1  config error (missing API key)
    2  transient network failure (will retry next hour)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Allow running from cron without changing CWD
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ── Constants (module-level, but no side-effects) ─────────────────────────

# Must stay the exact path app.transparency_routes.BACKFILL_SENTINEL_PATH reads
# (pinned by tests/test_drift_probe_rehome.py).
SENTINEL = Path("/var/lib/recipes-api/last_backfill_at")
KNOWN_DRIFT_LEDGER = Path(os.environ.get("DRIFT_PROBE_LEDGER", "/var/lib/recipes-api/drift-probe-known.json"))
MAX_REPORTS_PER_RUN = 5
DEFAULT_RATE_LIMIT_STATE = Path.home() / ".hermes" / "state" / "drift-probe-seen.json"
RATE_LIMIT_WINDOW_SECONDS = 24 * 3600

# 3370 = loopskill-api (systemd loopskill-api.service). 3360 is the SEPARATE
# recipes product; the pre-rehome default pointed the report at the wrong app.
API_BASE = os.environ.get("RECIPES_API_BASE", "http://127.0.0.1:3370")
WR_API_KEY = os.environ.get("WR_API_KEY", "")


# The schedule of record. Byte-identical to deploy/crontab.install-count-drift-probe
# (asserted by tests/test_drift_probe_rehome.py). Runs from the LIVE checkout and
# its `venv` (the dir loopskill-api.service itself execs; `.venv` exists too but
# the retired cron died on a hardcoded `.venv/bin/python`). Minute 12 is free of
# every other wired job. flock -E 4 = a stalled run never stacks, and the skip
# leaves a marker instead of vanishing.
CRONTAB_LINE = (
    "12 * * * * cd /home/wisechef/loopskill-api || exit 1; set -a; . ./.env; set +a; "
    "flock -n -E 4 /tmp/loopskill-install-count-drift-probe.lock "
    "venv/bin/python scripts/install_count_drift_probe.py "
    ">> /home/wisechef/wiserecipes-logs/install_count_drift_probe.log 2>&1; rc=$?; "
    '[ $rc -ne 4 ] || echo \'{"skipped": "lock held by a live run"}\' '
    ">> /home/wisechef/wiserecipes-logs/install_count_drift_probe.log 2>&1; exit $rc"
)


# ── Pure helpers (test surface for repohygiene_2605/A) ────────────────────


def compute_signature(slug: str) -> str:
    """Return a deterministic 16-char hex signature for ``(slug, install_count_drift)``.

    Replaces the pre-fix ``abs(hash(...)):016x`` formula, which depended on
    Python's salt-randomized builtin ``hash()`` and drifted across cron runs.
    """
    canonical = f"{slug}|install_count_drift".encode()
    return hashlib.sha256(canonical).hexdigest()[:16]


def limiter_signature(slug: str, actual: int, expected: int) -> str:
    """Rate-limiter key for ``(slug, actual, expected)``.

    The limiter must treat a CHANGED drift value as a new report: keyed on the
    slug alone, a slug reported once and then drifting to a different
    ``(actual, expected)`` inside the 24h window was held back until the window
    passed. The wire ``error_signature`` (``compute_signature``) deliberately stays
    value-blind so the tracker keeps deduping per slug; only this local key sees
    the values. ``|`` separates the numbers so ``(1, 23)`` and ``(12, 3)`` differ.
    """
    canonical = f"{slug}|install_count_drift|{int(actual)}|{int(expected)}".encode()
    return hashlib.sha256(canonical).hexdigest()[:16]


def should_report(
    slug: str,
    signature: str,
    *,
    state_path: Path | None = None,
    now_ts: float | None = None,
) -> bool:
    """Return True if ``(slug, signature)`` should be reported now, else False.

    Persistent 24h rate limit keyed on the (slug, signature) tuple. Backed by
    a tiny JSON state file at ``state_path`` (defaults to
    ``~/.hermes/state/drift-probe-seen.json``). Caller-injectable ``now_ts``
    keeps the function pure and testable.
    """
    if state_path is None:
        state_path = DEFAULT_RATE_LIMIT_STATE
    if now_ts is None:
        now_ts = time.time()

    state: dict[str, float] = {}
    if state_path.exists():
        try:
            raw = json.loads(state_path.read_text())
            if isinstance(raw, dict):
                # JSON only stores numbers, so cast back to float defensively.
                state = {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}
        except (OSError, ValueError, TypeError) as exc:
            # Rationale: state file is non-authoritative — corruption or partial
            # write must NOT crash the cron. Treat as "first run" and rebuild.
            logger.warning("drift-probe rate-limit state unreadable, rebuilding: %s", exc)
            state = {}

    key = f"{slug}::{signature}"
    last_seen = state.get(key)
    if last_seen is not None and (now_ts - last_seen) < RATE_LIMIT_WINDOW_SECONDS:
        return False

    # Update + persist. Garbage-collect entries older than 2× the window so the
    # file stays small even if many (slug, sig) pairs cycle through.
    cutoff = now_ts - 2 * RATE_LIMIT_WINDOW_SECONDS
    state = {k: v for k, v in state.items() if v >= cutoff}
    state[key] = now_ts
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, sort_keys=True))
    except OSError as exc:
        # Rationale: failing to PERSIST the rate limit is non-fatal — we'd
        # rather re-report than crash; a parallel cron will retry on next tick.
        logger.warning("drift-probe could not persist rate-limit state: %s", exc)
    return True


# ── Known-drift ledger + report planner (t_a1edaf10) ─────────────────────

Drift = tuple[int, int]  # (actual counter, expected truth)


def load_known(path: Path) -> dict[str, Drift] | None:
    """Return the ledger, or None when it is absent OR unreadable/malformed.

    None means "treat this as a first run": the caller re-seeds the baseline and
    reports nothing. Corruption must never read as an empty ledger — that would
    make every drifting slug look new and flood the tracker.
    """
    try:
        raw = json.loads(path.read_text())
        known = raw["known"]
        if not isinstance(known, dict):
            return None
        return {str(slug): (int(v[0]), int(v[1])) for slug, v in known.items()}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        logger.warning("drift-probe known-drift ledger unreadable, re-seeding: %s", exc)
        return None


def save_known(path: Path, known: dict[str, Drift]) -> bool:
    """Atomically persist the ledger (tmp + replace). False when it cannot."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"known": {k: list(v) for k, v in sorted(known.items())}}, sort_keys=True))
        os.replace(tmp, path)
        return True
    except OSError as exc:
        logger.warning("drift-probe could not persist known-drift ledger %s: %s", path, exc)
        return False


def plan_reports(drifts: dict[str, Drift], known: dict[str, Drift]) -> list[str]:
    """Candidate slugs, sorted: new or changed vs ``known``.

    Pure: no I/O, inputs untouched, deterministic order (so a deferred slug is
    picked up predictably by the next run). The per-run CAP is applied by the
    caller, on POSTs actually attempted — a slug the 24h rate limit suppresses
    sends nothing and must not use up a cap slot.
    """
    return sorted(slug for slug, value in drifts.items() if known.get(slug) != value)


def _write_sentinel() -> None:
    """Atomic: the endpoint must never read a half-written timestamp."""
    tmp = SENTINEL.with_name(SENTINEL.name + ".tmp")
    try:
        tmp.write_text(datetime.now(timezone.utc).isoformat())
        os.replace(tmp, SENTINEL)
    except OSError as exc:
        # Rationale: sentinel write is observability, not correctness — must
        # not crash the probe in dev/test envs without /var/lib perms.
        logger.warning("could not update sentinel %s: %s", SENTINEL, exc)


# ── DB + network ─────────────────────────────────────────────────────────


def _open_session():
    """Lazy DB import so module-level import works in environments without app/."""
    from app.database import SessionLocal  # noqa: WPS433 — lazy by design

    return SessionLocal()


def compute_truth(db) -> dict[str, int]:
    """Return {slug: max(telemetry_installs, organic_install_events)} per slug.

    chef_0823 (t_4a38fed9): InstallEvent truth now uses the ONE shared organic
    predicate (app/install_integrity.py) — the same definition that gates the
    denormalised Skill.install_count bump. Without this, the counter re-sync
    (which drops internal/CI/agent-probe installs) would file hourly drift
    reports forever. Telemetry rows carry no client_ip provenance reliable
    enough to filter identically, so telemetry counts stay raw — the probe
    takes max(), and telemetry only ever pushes truth UP, never down.
    """
    from sqlalchemy import func  # noqa: WPS433 — lazy
    from app.install_integrity import organic_install_predicate  # noqa: WPS433 — lazy
    from app.models import (  # noqa: WPS433 — lazy
        APIKey,
        InstallEvent,
        TelemetryEvent,
        User,
    )

    truth: dict[str, int] = {}
    for slug, count in (
        db.query(TelemetryEvent.skill_slug, func.count())
        .filter(TelemetryEvent.event_type == "install")
        .group_by(TelemetryEvent.skill_slug)
        .all()
    ):
        if slug:
            truth[slug] = max(truth.get(slug, 0), count or 0)
    for slug, count in (
        db.query(InstallEvent.skill_slug, func.count())
        .outerjoin(APIKey, APIKey.id == InstallEvent.api_key_id)
        .outerjoin(User, User.id == APIKey.user_id)
        .filter(*organic_install_predicate())
        .group_by(InstallEvent.skill_slug)
        .all()
    ):
        if slug:
            truth[slug] = max(truth.get(slug, 0), count or 0)
    return truth


def report_drift(slug: str, actual: int, expected: int) -> bool:
    """POST to /api/v1/skill-error. Returns True on success."""
    if not WR_API_KEY:
        # No API key in env — log and skip (do not crash cron).
        logger.warning("WR_API_KEY missing; cannot report drift on %s", slug)
        return False
    payload = {
        "skill_slug": slug,
        "error_signature": compute_signature(slug),
        "env_fingerprint": {
            "host": os.uname().nodename[:64],
            "actual": str(actual)[:64],
            "expected": str(expected)[:64],
        },
        "agent_fp_anon": "drift-probe-cron"[:64].ljust(8, "0"),
        "command": "scripts/install_count_drift_probe.py",
        "exit_code": 0,
        "stack_trace_top": "",
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{API_BASE}/api/v1/skill-error",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": WR_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — internal cron URL
            r.read()
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            logger.info("drift report opt-in disabled (RECIPES_REPORT_ERRORS=false); skipping")
            return False
        logger.warning("drift report HTTP %s: %s", exc.code, exc.reason)
        return False
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("drift report transient failure: %s", exc)
        return False


def collect_drifts(db, truth: dict[str, int]) -> dict[str, Drift]:
    """{slug: (counter, truth)} for every skill whose counter disagrees."""
    from app.models import Skill  # noqa: WPS433 — lazy

    drifts: dict[str, Drift] = {}
    for skill in db.query(Skill).all():
        actual = int(skill.install_count or 0)
        expected = int(truth.get(skill.slug, 0))
        if actual != expected:
            drifts[skill.slug] = (actual, expected)
    return drifts


def _report_planned(drifts: dict[str, Drift], known: dict[str, Drift]) -> tuple[dict[str, Drift], int, int]:
    """Report what the gate allows. Returns (new ledger, reported, suppressed)."""
    new_known = {slug: v for slug, v in known.items() if slug in drifts}
    attempted = reported = suppressed = 0
    for slug in plan_reports(drifts, known):
        if attempted >= MAX_REPORTS_PER_RUN:
            break  # the rest stay out of the ledger: deferred to a later run, not lost
        actual, expected = drifts[slug]
        if not should_report(slug, limiter_signature(slug, actual, expected)):
            suppressed += 1
            logger.info("drift on %s suppressed by 24h rate limit", slug)
            continue
        attempted += 1
        if report_drift(slug, actual, expected):
            reported += 1
            new_known[slug] = drifts[slug]  # only a delivered report marks it known
        else:
            logger.info("drift on %s: actual=%d expected=%d (not reported)", slug, actual, expected)
    return new_known, reported, suppressed


def main() -> int:
    """Cron entry point. Returns shell-style exit code."""
    # Sentinel dir creation deferred from import-time to runtime so tests + dev
    # environments can import this module without /var/lib/recipes-api perms.
    try:
        SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("sentinel dir not writable (non-prod env?): %s", exc)

    db = _open_session()
    try:
        drifts = collect_drifts(db, compute_truth(db))
        total_drift = sum(abs(a - e) for a, e in drifts.values())
        known = load_known(KNOWN_DRIFT_LEDGER)
        reported = suppressed = 0
        if known is None:
            # BASELINE: first run (or corrupt ledger). Record, report nothing.
            seeded = save_known(KNOWN_DRIFT_LEDGER, drifts)
            logger.info(
                "drift baseline %s: %d legacy drifting slugs recorded, 0 reported",
                "seeded" if seeded else "NOT persisted (fail-closed)",
                len(drifts),
            )
        elif not save_known(KNOWN_DRIFT_LEDGER, known):
            # Fail CLOSED on every run, not only the baseline: prove the ledger
            # can be written BEFORE any POST. A run that reports and then cannot
            # record it would re-report the same slugs every hour.
            logger.warning(
                "drift ledger %s not writable: %d drifting slugs, 0 reported (fail-closed)",
                KNOWN_DRIFT_LEDGER,
                len(drifts),
            )
        else:
            new_known, reported, suppressed = _report_planned(drifts, known)
            if not save_known(KNOWN_DRIFT_LEDGER, new_known):
                logger.error("drift ledger write failed AFTER %d reports; they may repeat next run", reported)
        _write_sentinel()
        logger.info(
            "drift probe complete: total_drift=%d reported=%d suppressed=%d",
            total_drift,
            reported,
            suppressed,
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
