"""Public transparency endpoint — LoopSkill platform health scorecard.

Exposes aggregate, no-PII metrics that show whether the feedback loop is
actually working: install-count drift, skill-error rate, feedback volume,
median issue resolution time, last backfill timestamp.

The point is operational honesty — if anything looks bad, that means we
have a real problem to fix. Visibility forces honesty.

Schema (response):
    {
      "install_count_drift": int,         # |denormalised - recomputed|, summed across skills
      "skill_error_rate_7d": float,       # skill_errors / (telemetry installs in 7d), 0..1
                                          # ONLY incident_reports rows whose report_type is
                                          # 'skill_error' (an agent saying a skill it ran
                                          # misbehaved) — NOT crash reports; see below
      "crash_report_count_7d": int,       # incident_reports rows with report_type 'incident'
                                          # (POST /api/feedback/incident — the auto-improve
                                          # tooling crashed). Split out of the numerator
                                          # above on 2026-11-16 (t_a0607d92); additive.
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

import json
import logging
import os
import statistics
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
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

from app.version import __version__ as VERSION


@router.get("")
def health_status() -> dict[str, Any]:
    """Public liveness status — no auth, no DB dependency.

    spotify_1507 Phase 0: the bare ``GET /api/health`` used to 401 (only the
    ``/api/health/transparency`` sub-route was public), which read as a trust
    leak on the cold-agent path — a status URL should never demand a key. This
    returns a minimal, always-200, DB-independent liveness surface (same
    disclosure level as a public status page). Detailed operational metrics
    remain at ``/api/health/transparency``; DB liveness at ``/api/healthz``.
    """
    return {
        "status": "ok",
        "service": "loopskill-api",
        "version": VERSION,
    }


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
        db.query(InstallEvent.skill_slug, func.count().label("c")).group_by(InstallEvent.skill_slug).all()
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


def _installs_7d(db: Session) -> int:
    """Telemetry ``install`` events inside the 7d window — the rate's denominator.

    Extracted verbatim from :func:`_compute_skill_error_rate_7d`; the query is
    deliberately UNCHANGED (out of scope for t_a0607d92 — see that function's
    docstring for what is wrong with it). It lives here so the denominator the
    metric publishes is one named thing a test can pin, not an inline expression.
    """
    cutoff = datetime.now(UTC) - timedelta(days=7)
    return (
        db.query(func.count(TelemetryEvent.id))
        .filter(TelemetryEvent.event_type == "install")
        .filter(TelemetryEvent.created_at >= cutoff)
        .scalar()
        or 0
    )


def _incident_report_counts_7d(db: Session) -> tuple[int, int]:
    """(skill_errors, crash_reports) inside the 7d window — one query, one guard.

    Why one helper for both halves: they used to be ONE undifferentiated count.
    ``incident_reports`` holds two populations with two different meanings
    (crash reports from ``POST /api/feedback/incident`` and agent skill errors
    from ``POST /api/v1/skill-error`` + MCP ``loopskill_report_skill_error``) and
    the public metric is named after only the second — see the IncidentReport
    class docstring. Deriving both from a single grouped query means the metric
    and its companion can never be read from different snapshots, and neither
    half can silently degrade to 0 while the other still looks healthy.

    The filter is on ``occurred_at``, the model's only timestamp column — there
    is no ``created_at`` on this table. The guard catches ONLY a genuinely absent
    table (a missing table is legitimately a 0 on an older self-host DB); a real
    coding error propagates instead of being laundered into a healthy-looking
    zero, which is the entire point of a transparency endpoint.
    """
    cutoff = datetime.now(UTC) - timedelta(days=7)
    from app.models import IncidentReport

    try:
        rows = (
            db.query(IncidentReport.report_type, func.count(IncidentReport.id))
            .filter(IncidentReport.occurred_at >= cutoff)
            .group_by(IncidentReport.report_type)
            .all()
        )
    except SQLAlchemyError:
        return 0, 0

    skill_errors = crash_reports = 0
    for report_type, count in rows:
        if report_type == IncidentReport.TYPE_SKILL_ERROR:
            skill_errors += int(count or 0)
        else:
            # Anything that is not provably a skill error — a legacy row the
            # backfill could not prove, or a future rail that forgot to classify
            # and inherited the server_default — counts as a crash report, NOT as
            # a skill error. Deliberately biased this way: a skill-error rate
            # inflated by crashes misleads whoever is judging a creator's skill on
            # a public page, while an understated one only hides a problem that is
            # still visible in crash_report_count_7d.
            crash_reports += int(count or 0)
    return skill_errors, crash_reports


def _skill_error_rate_from_counts(skill_errors: int, installs: int) -> float:
    """skill_errors / installs, 0..1. Returns 0.0 if the denominator is 0."""
    if installs <= 0:
        return 0.0
    return round(min(1.0, skill_errors / installs), 6)


def _compute_skill_error_rate_7d(db: Session) -> float:
    """skill_errors_7d / installs_7d, 0..1. Returns 0.0 if denominator is 0.

    Numerator counts ONLY rows with ``report_type == TYPE_SKILL_ERROR``. Counting
    the whole ``incident_reports`` table — which is what this did — published the
    union of crash reports and skill errors under a skill-error name. It had
    never been observable because the ``created_at``/``AttributeError`` bug pinned
    the metric at 0.0 for its entire existence (see the IncidentReport docstring).

    OUT OF SCOPE, stated so rather than left implicit: the DENOMINATOR is
    untouched here. It is ``TelemetryEvent`` rows with ``event_type='install'``
    in the same 7d window. That choice has its own problems — measured on prod
    2026-11-16 it reads 0 installs/7d against 120 all-time, so the metric returns
    0.0 by the denominator guard regardless of the numerator, and telemetry
    installs are not the same population as ``InstallEvent`` — but retuning it
    would change a second public contract at the same time as this one fixes the
    first. It gets its own card.
    """
    errors, _crashes = _incident_report_counts_7d(db)
    return _skill_error_rate_from_counts(errors, _installs_7d(db))


def _compute_crash_report_count_7d(db: Session) -> int:
    """Crash reports (``POST /api/feedback/incident``) in the window.

    Published alongside the skill-error rate so that separating the two
    populations does not quietly delete a signal from the transparency
    scorecard: this number was always inside ``skill_error_rate_7d``'s numerator,
    just filed under the wrong name.
    """
    _skill_errors, crashes = _incident_report_counts_7d(db)
    return crashes


def _compute_feedback_volume_7d(db: Session) -> int:
    if FeedbackSubmission is None:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=7)
    return (
        db.query(func.count(FeedbackSubmission.id)).filter(FeedbackSubmission.created_at >= cutoff).scalar()
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
    cutoff = datetime.now(UTC) - timedelta(days=30)
    q = (
        "repo:wisechef-ai/loopskill-api"
        " is:issue is:closed label:agent-reported"
        f" closed:>{cutoff.strftime('%Y-%m-%d')}"
    )
    url = "https://api.github.com/search/issues?per_page=100&q=" + urllib.parse.quote(q, safe="")
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
    # ONE grouped read of incident_reports for both halves of the split, so the
    # skill-error rate and the crash count can never be published from different
    # snapshots of the table (each _compute_* helper on its own would query twice
    # and could straddle an insert).
    skill_errors, crash_reports = _incident_report_counts_7d(db)
    return {
        "install_count_drift": _compute_install_count_drift(db),
        "skill_error_rate_7d": _skill_error_rate_from_counts(skill_errors, _installs_7d(db)),
        # The crash-report half of incident_reports, which until t_a0607d92 was
        # counted INSIDE skill_error_rate_7d. Separating the two populations must
        # not silently drop one of them off the scorecard — this keeps the number
        # visible and puts it under its real name.
        "crash_report_count_7d": crash_reports,
        "feedback_volume_7d": _compute_feedback_volume_7d(db),
        "median_issue_resolution_h": _compute_median_issue_resolution_h(),
        "last_backfill_at": (_compute_last_backfill_at() or None),
        "computed_at": datetime.now(UTC).isoformat(),
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
