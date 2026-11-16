"""B.4 — Hourly clustering cron `recipes-incident-clustering`.

Groups incident_reports from the last 24h by (skill_id, error_signature)
and upserts a row into patch_candidates whenever a cluster crosses the
threshold:

    COUNT(*) >= 3
    AND COUNT(DISTINCT agent_fp_anon) >= 3

PROVENANCE (t_0111f6c4, 2026-11-16): counts only rows that pass
``IncidentReport.skill_error_rows()``. A patch_candidate is a request to patch
a *skill's* source — `patch_drafter` pulls the skill tarball and the canary
STATIC gate runs the drafted test against old + new skill source — so the only
valid evidence for one is an agent reporting that a skill misbehaved. Crash
reports from ``POST /api/feedback/incident`` are LoopSkill's own tooling dying;
they name a ``skill_id`` because the column is NOT NULL, not because the skill
did anything, and clustering them would mint a candidate asking for a patch to
the wrong codebase. The filter is applied via the shared classmethod rather
than a literal so this cron and ``patch_drafter._top_reports`` cannot drift:
they read the same (skill_id, signature) pair and MUST agree on what it means.

Existing patch_candidate rows for the same (skill_id, signature) are
updated in place — status only escalates from `pending`. Clusters that
have already entered the canary pipeline keep their downstream status.

Run as `python -m app.crons.incident_clustering` from a systemd timer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import IncidentReport, PatchCandidate

log = logging.getLogger("recipes.incident_clustering")

CLUSTER_THRESHOLD = 3
WINDOW_HOURS = 24


@dataclass
class Cluster:
    skill_id: object  # UUID — kept as native type for FK comparisons
    error_signature: str
    cluster_count: int
    distinct_agents: int


def find_clusters(
    db: Session,
    *,
    now: datetime | None = None,
    threshold: int = CLUSTER_THRESHOLD,
    window_hours: int = WINDOW_HOURS,
) -> list[Cluster]:
    """Return clusters whose count and distinct-agent count both meet threshold.

    Skill-error provenance only — see the module docstring for why a crash
    report may not seed a ``PatchCandidate``.

    Why filter rather than "cluster each type separately": ``PatchCandidate``'s
    natural key is ``uq_patch_candidate_sig (skill_id, error_signature)`` and it
    has no report_type column. Two per-type clusters over the same pair would
    collide on that constraint and ``upsert_candidate`` would silently overwrite
    one type's metrics with the other's. A per-type lane is a schema change
    (column + widened unique constraint + migration + a consumer that can act on
    crash-report clusters, which today nothing is), not a filter tweak — so it is
    out of scope here, and excluding the useless population beats pretending to
    cluster it.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=window_hours)

    rows = (
        db.query(
            IncidentReport.skill_id,
            IncidentReport.error_signature,
            func.count(IncidentReport.id).label("cluster_count"),
            func.count(func.distinct(IncidentReport.agent_fp_anon)).label("distinct_agents"),
        )
        .filter(IncidentReport.occurred_at > cutoff)
        .filter(IncidentReport.skill_error_rows())
        .group_by(IncidentReport.skill_id, IncidentReport.error_signature)
        .having(func.count(func.distinct(IncidentReport.agent_fp_anon)) >= threshold)
        .all()
    )
    return [
        Cluster(
            skill_id=r.skill_id,
            error_signature=r.error_signature,
            cluster_count=int(r.cluster_count),
            distinct_agents=int(r.distinct_agents),
        )
        for r in rows
    ]


def upsert_candidate(db: Session, cluster: Cluster, *, now: datetime | None = None) -> PatchCandidate:
    """Insert or update a patch_candidate for this cluster.

    Status only flips to 'pending' when a row is being created. If a
    candidate already exists in any non-terminal state we leave its
    status alone — only the metrics + last_clustered_at advance.
    """
    now = now or datetime.now(UTC)
    existing = (
        db.query(PatchCandidate)
        .filter(
            PatchCandidate.skill_id == cluster.skill_id,
            PatchCandidate.error_signature == cluster.error_signature,
        )
        .first()
    )
    if existing is None:
        existing = PatchCandidate(
            skill_id=cluster.skill_id,
            error_signature=cluster.error_signature,
            cluster_count=cluster.cluster_count,
            distinct_agents=cluster.distinct_agents,
            status="pending",
            last_clustered_at=now,
        )
        db.add(existing)
    else:
        existing.cluster_count = cluster.cluster_count
        existing.distinct_agents = cluster.distinct_agents
        existing.last_clustered_at = now
    return existing


def run_once(db: Session | None = None, *, now: datetime | None = None) -> int:
    own_session = db is None
    db = db or SessionLocal()
    try:
        clusters = find_clusters(db, now=now)
        for c in clusters:
            upsert_candidate(db, c, now=now)
        db.commit()
        log.info("clustered %d (skill,sig) groups above threshold", len(clusters))
        return len(clusters)
    finally:
        if own_session:
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_once()
