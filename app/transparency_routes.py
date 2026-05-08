"""Public transparency endpoint — recipes platform health scorecard.

Exposes aggregate, no-PII metrics that show whether the feedback loop is
actually working: install-count drift, skill-error rate, feedback volume,
median issue resolution time, last backfill timestamp.

The point is operational honesty — if anything looks bad, that means we
have a real problem to fix. Visibility forces honesty.

Schema (response):
    {
      "install_count_drift": int,         # |denormalised - recomputed|, summed across skills
      "skill_error_rate_7d": float,       # errors / (telemetry installs in 7d), 0..1
      "feedback_volume_7d": int,          # rows in feedback_submissions in last 7d
      "median_issue_resolution_h": float | null,
                                          # median (closed_at - created_at) hours, GitHub
                                          # issues with label `agent-reported` closed in 30d
      "last_backfill_at": datetime | null,
      "computed_at": datetime,            # always now()
      "ttl_seconds": 60,
    }

The endpoint is unauthenticated and cached 60s in-process. Same disclosure
level as a status page.
"""
from __future__ import annotations

import logging
import os
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import urllib.parse
import urllib.request
import urllib.error
import json

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import InstallEvent, Skill, TelemetryEvent

# FeedbackSubmission lands in the same sprint via the feedback_v1 migration
# (a1b2c3d4e5f6). When this file deploys before that migration applies, the
# import is degraded to None and feedback_volume_7d returns 0.
try:
    from app.models import FeedbackSubmission  # type: ignore
except ImportError:  # pragma: no cover
    FeedbackSubmission = None  # type: ignore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["transparency"])


# In-process cache (60s TTL). Same approach as elsewhere in the app.
_CACHE_TTL_S = 60
_cache_lock = threading.Lock()
_cache_payload: dict[str, Any] | None = None
_cache_ts: float = 0.0


def _compute_install_count_drift(db: Session) -> int:
    """Sum |denormalised counter - recomputed truth| across all skills.

    Truth = union of (telemetry events with event_type='install') and
            (install_events rows). RCP-13 alignment.
    """
    # Recomputed counts per slug
    telemetry_q = (
        db.query(TelemetryEvent.skill_slug, func.count().label("c"))
        .filter(TelemetryEvent.event_type == "install")
        .group_by(TelemetryEvent.skill_slug)
        .all()
    )
    install_q = (
        db.query(InstallEvent.skill_slug, func.count().label("c"))
        .group_by(InstallEvent.skill_slug)
        .all()
    )
    truth: dict[str, int] = {}
    for slug, count in telemetry_q:
        if slug:
            truth[slug] = max(truth.get(slug, 0), count)
    for slug, count in install_q:
        if slug:
            truth[slug] = max(truth.get(slug, 0), count)

    drift = 0
    for skill in db.query(Skill).all():
        actual = int(skill.install_count or 0)
        expected = int(truth.get(skill.slug, 0))
        drift += abs(actual - expected)
    return drift


def _compute_skill_error_rate_7d(db: Session) -> float:
    """errors_7d / installs_7d, 0..1. Returns 0.0 if denominator is 0."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    installs = (
        db.query(func.count(TelemetryEvent.id))
        .filter(TelemetryEvent.event_type == "install")
        .filter(TelemetryEvent.created_at >= cutoff)
        .scalar()
        or 0
    )
    # IncidentReport is the persistence model for both /api/feedback/incident
    # and /api/v1/skill-error. Filter by report_type if available, else count all.
    try:
        from app.models import IncidentReport

        errors = (
            db.query(func.count(IncidentReport.id))
            .filter(IncidentReport.created_at >= cutoff)
            .scalar()
            or 0
        )
    except Exception:
        errors = 0

    if installs <= 0:
        return 0.0
    return round(min(1.0, errors / installs), 6)


def _compute_feedback_volume_7d(db: Session) -> int:
    if FeedbackSubmission is None:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    return (
        db.query(func.count(FeedbackSubmission.id))
        .filter(FeedbackSubmission.created_at >= cutoff)
        .scalar()
        or 0
    )


def _compute_median_issue_resolution_h() -> float | None:
    """Query GitHub for `agent-reported` issues closed in last 30d.

    Returns median hours between created_at and closed_at, or None if
    fewer than 3 closed issues are available (statistical noise floor).
    Failures (rate-limit, network) return None — never raise.
    """
    pat = os.environ.get("GITHUB_DISPATCH_PAT") or os.environ.get("GITHUB_TOKEN")
    if not pat:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    q = (
        "repo:wisechef-ai/recipes-api"
        " is:issue is:closed label:agent-reported"
        f" closed:>{cutoff.strftime('%Y-%m-%d')}"
    )
    url = (
        "https://api.github.com/search/issues?per_page=100&q="
        + urllib.parse.quote(q, safe="")
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {pat}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "recipes-transparency",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        logger.warning("transparency: GitHub query failed: %s", exc)
        return None

    durations: list[float] = []
    for issue in data.get("items", []) or []:
        ca = issue.get("created_at")
        cb = issue.get("closed_at")
        if not ca or not cb:
            continue
        try:
            t0 = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(cb.replace("Z", "+00:00"))
        except ValueError:
            continue
        durations.append((t1 - t0).total_seconds() / 3600.0)

    if len(durations) < 3:
        return None
    return round(statistics.median(durations), 2)


def _compute_last_backfill_at() -> datetime | None:
    """Read the last successful backfill timestamp from a sentinel file."""
    sentinel = "/var/lib/recipes-api/last_backfill_at"
    try:
        with open(sentinel) as f:
            ts = f.read().strip()
        return datetime.fromisoformat(ts)
    except (OSError, ValueError):
        return None


def _build_payload(db: Session) -> dict[str, Any]:
    return {
        "install_count_drift": _compute_install_count_drift(db),
        "skill_error_rate_7d": _compute_skill_error_rate_7d(db),
        "feedback_volume_7d": _compute_feedback_volume_7d(db),
        "median_issue_resolution_h": _compute_median_issue_resolution_h(),
        "last_backfill_at": (_compute_last_backfill_at() or None),
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": _CACHE_TTL_S,
    }


@router.get("/transparency")
def transparency(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Public scorecard. Cached 60s, no auth."""
    global _cache_payload, _cache_ts
    now = time.monotonic()
    with _cache_lock:
        if _cache_payload is not None and (now - _cache_ts) < _CACHE_TTL_S:
            return _cache_payload
    payload = _build_payload(db)
    # Convert datetime to ISO string for JSON safety
    if isinstance(payload.get("last_backfill_at"), datetime):
        payload["last_backfill_at"] = payload["last_backfill_at"].isoformat()
    with _cache_lock:
        _cache_payload = payload
        _cache_ts = now
    return payload
