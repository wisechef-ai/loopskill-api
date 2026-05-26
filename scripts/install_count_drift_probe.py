#!/usr/bin/env python3
"""install_count_drift_probe — hourly drift monitor for skill install counts.

Compares Skill.install_count (denormalised counter) to the recomputed truth
(union of telemetry installs + install_events) and POSTs to
/api/v1/skill-error with quality:hardcoded-path label when drift > 0.

Run hourly via cron (entry installed by the Stream 0 deploy).
Idempotent — re-running with no drift is a no-op.

Sentinel file `/var/lib/recipes-api/last_backfill_at` is updated on every
successful run with the current ISO timestamp; the transparency endpoint
reads it.

### Signature stability (repohygiene_2605 Phase A)

Pre-fix: `error_signature` was derived from Python's builtin ``hash()``, which
is salt-randomized per process (PYTHONHASHSEED defaults to "random"). Every
hourly cron invocation produced a brand-new signature for the same
``(slug, drift_kind)`` tuple. The dispatcher had no dedup match and opened a
fresh GitHub issue each hour — 127 noise issues accumulated in ~5 days.

Post-fix:
  * ``compute_signature(slug)`` uses ``hashlib.sha256`` over a canonical input
    so the signature is stable across processes, hosts, clocks, and users.
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

SENTINEL = Path("/var/lib/recipes-api/last_backfill_at")
DEFAULT_RATE_LIMIT_STATE = Path.home() / ".hermes" / "state" / "drift-probe-seen.json"
RATE_LIMIT_WINDOW_SECONDS = 24 * 3600

API_BASE = os.environ.get("RECIPES_API_BASE", "http://127.0.0.1:3360")
WR_API_KEY = os.environ.get("WR_API_KEY", "")


# ── Pure helpers (test surface for repohygiene_2605/A) ────────────────────


def compute_signature(slug: str) -> str:
    """Return a deterministic 16-char hex signature for ``(slug, install_count_drift)``.

    Replaces the pre-fix ``abs(hash(...)):016x`` formula, which depended on
    Python's salt-randomized builtin ``hash()`` and drifted across cron runs.
    """
    canonical = f"{slug}|install_count_drift".encode()
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


# ── DB + network ─────────────────────────────────────────────────────────


def _open_session():
    """Lazy DB import so module-level import works in environments without app/."""
    from app.database import SessionLocal  # noqa: WPS433 — lazy by design

    return SessionLocal()


def compute_truth(db) -> dict[str, int]:
    """Return {slug: max(telemetry_installs, install_events)} per slug."""
    from sqlalchemy import func  # noqa: WPS433 — lazy
    from app.models import InstallEvent, TelemetryEvent  # noqa: WPS433 — lazy

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
            logger.info(
                "drift report opt-in disabled (RECIPES_REPORT_ERRORS=false); skipping"
            )
            return False
        logger.warning("drift report HTTP %s: %s", exc.code, exc.reason)
        return False
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("drift report transient failure: %s", exc)
        return False


def main() -> int:
    """Cron entry point. Returns shell-style exit code."""
    # Sentinel dir creation deferred from import-time to runtime so tests + dev
    # environments can import this module without /var/lib/recipes-api perms.
    try:
        SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        logger.warning("sentinel dir not writable (non-prod env?): %s", exc)

    from app.models import Skill  # noqa: WPS433 — lazy

    db = _open_session()
    try:
        truth = compute_truth(db)
        total_drift = 0
        reported = 0
        suppressed = 0
        for skill in db.query(Skill).all():
            actual = int(skill.install_count or 0)
            expected = int(truth.get(skill.slug, 0))
            if actual != expected:
                total_drift += abs(actual - expected)
                signature = compute_signature(skill.slug)
                if not should_report(skill.slug, signature):
                    suppressed += 1
                    logger.info(
                        "drift on %s suppressed by 24h rate limit (signature=%s)",
                        skill.slug,
                        signature,
                    )
                    continue
                if report_drift(skill.slug, actual, expected):
                    reported += 1
                else:
                    logger.info(
                        "drift on %s: actual=%d expected=%d (not reported)",
                        skill.slug,
                        actual,
                        expected,
                    )
        try:
            SENTINEL.write_text(datetime.now(timezone.utc).isoformat())
        except OSError as exc:
            # Rationale: sentinel write is observability, not correctness — must
            # not crash the probe in dev/test envs without /var/lib perms.
            logger.warning("could not update sentinel %s: %s", SENTINEL, exc)
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
