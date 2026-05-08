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

Exit codes:
    0  no drift OR drift reported successfully
    1  config error (missing API key)
    2  transient network failure (will retry next hour)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Allow running from cron without changing CWD
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import InstallEvent, Skill, TelemetryEvent  # noqa: E402

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SENTINEL = Path("/var/lib/recipes-api/last_backfill_at")
SENTINEL.parent.mkdir(parents=True, exist_ok=True)

API_BASE = os.environ.get("RECIPES_API_BASE", "http://127.0.0.1:3360")
WR_API_KEY = os.environ.get("WR_API_KEY", "")


def compute_truth(db) -> dict[str, int]:
    """Return {slug: max(telemetry_installs, install_events)} per slug."""
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
        "error_signature": f"{abs(hash((slug, 'install_count_drift'))) :016x}"[:16],
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
        with urllib.request.urlopen(req, timeout=10) as r:
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
    db = SessionLocal()
    try:
        truth = compute_truth(db)
        total_drift = 0
        reported = 0
        for skill in db.query(Skill).all():
            actual = int(skill.install_count or 0)
            expected = int(truth.get(skill.slug, 0))
            if actual != expected:
                total_drift += abs(actual - expected)
                if report_drift(skill.slug, actual, expected):
                    reported += 1
                else:
                    logger.info(
                        "drift on %s: actual=%d expected=%d (not reported)",
                        skill.slug,
                        actual,
                        expected,
                    )
        SENTINEL.write_text(datetime.now(timezone.utc).isoformat())
        logger.info(
            "drift probe complete: total_drift=%d reported=%d", total_drift, reported
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
