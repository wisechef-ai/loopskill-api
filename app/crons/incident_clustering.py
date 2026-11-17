"""B.4 — Hourly incident clustering (was: the `recipes-` prefixed unit name).

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

WIRING — crontab, not the systemd unit that shipped with it (t_f1867996)
------------------------------------------------------------------------
This docstring used to say "Run as ``python -m app.crons.incident_clustering``
from a systemd timer". Its two systemd unit files existed in ``deploy/`` and
were never installed: ``systemctl list-timers --all`` on wisechef-hq listed
zero loopskill/recipes units, and the unit started
``/srv/recipes-api/.venv/bin/python``, a prefix that does not exist on that
host. Outcome evidence that it never ran: measured 2026-11-16,
``patch_candidates`` held **0 rows** and ``max(last_clustered_at)`` was NULL —
no clustering pass in the table's history. The units are deleted in this commit;
the schedule of record is ``CRONTAB_LINE`` below, mirrored byte-for-byte into
``deploy/crontab.incident-clustering`` and asserted equal by
``tests/test_crons_wiring.py``.

Run manually with ``python -m app.crons.incident_clustering`` (hourly via cron).
Exit codes: 0 = ran (0 clusters is a healthy result, not a failure), 1 = error,
2 = refused an unsafe parameter, 3 = usage error; 4 is reserved for the cron
lock and this job must never return it (see app/crons/_cli.py).
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.crons._cli import Parser as CliParser, positive_int
from app.database import SessionLocal
from app.models import IncidentReport, PatchCandidate

log = logging.getLogger("recipes.incident_clustering")

CLUSTER_THRESHOLD = 3
WINDOW_HOURS = 24

# The schedule of record — mirrored byte-for-byte into
# deploy/crontab.incident-clustering and asserted equal by
# tests/test_crons_wiring.py. Hourly, matching the intent of the
# deleted 1h-interval timer that shipped with this module. :07 clears the :02
# drain, the :05 postiz monitor and the :10 claimgate sync.
#
# Hourly is also where a lock collision is genuinely likely, which is why this
# line — not just this job — was rewritten. ONE `flock -n -E 4`: the reserved
# exit 4, the measured silence of plain flock, and why the skip marker is guarded
# rather than a bare `||`, are all in app/crons/_cli.
CRONTAB_LINE = (
    "7 * * * * cd /home/wisechef/loopskill-api && set -a; . ./.env; set +a; "
    "flock -n -E 4 /tmp/loopskill-incident-clustering.lock venv/bin/python -m app.crons.incident_clustering >> /home/wisechef/wiserecipes-logs/incident_clustering.log 2>&1; "
    'rc=$?; [ $rc -ne 4 ] || echo \'{"skipped": "lock held by a live run"}\' >> /home/wisechef/wiserecipes-logs/incident_clustering.log 2>&1; '
    "exit $rc"
)


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


def run_once(
    db: Session | None = None,
    *,
    now: datetime | None = None,
    threshold: int = CLUSTER_THRESHOLD,
    window_hours: int = WINDOW_HOURS,
) -> int:
    own_session = db is None
    db = db or SessionLocal()
    try:
        clusters = find_clusters(db, now=now, threshold=threshold, window_hours=window_hours)
        for c in clusters:
            upsert_candidate(db, c, now=now)
        db.commit()
        log.info("clustered %d (skill,sig) groups above threshold", len(clusters))
        return len(clusters)
    finally:
        if own_session:
            db.close()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point — this is what cron runs, so it is what the tests pin.

    Zero clusters is a healthy outcome, not a failure: with a quiet incident
    stream the correct behaviour is to log ``clusters: 0`` and exit 0. A job
    that exits non-zero on "nothing to do" trains everyone to ignore the log.
    """
    ap = CliParser(
        prog="incident_clustering",
        description="Cluster incident_reports into patch_candidates (B.4).",
    )
    ap.add_argument(
        "--hours",
        type=int,
        default=WINDOW_HOURS,
        help=f"look-back window in hours (default {WINDOW_HOURS})",
    )
    ap.add_argument(
        "--threshold",
        type=int,
        default=CLUSTER_THRESHOLD,
        help=f"min count AND min distinct agents per cluster (default {CLUSTER_THRESHOLD})",
    )
    args = ap.parse_args(argv)
    # Both go through the same floor the pruners use. A zero/negative threshold
    # pushes `HAVING COUNT(DISTINCT agent_fp_anon) >= 0`, which qualifies EVERY
    # group in the window — single-report, single-agent noise becomes `pending`
    # patch candidates and lands in the automated patch/canary queue the >= 3
    # bar exists to keep out. A zero window reads as `clusters: 0` + exit 0,
    # i.e. it looks healthy while doing nothing.
    threshold = positive_int("threshold", args.threshold, minimum=1)
    hours = positive_int("hours", args.hours, minimum=1)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        clusters = run_once(threshold=threshold, window_hours=hours)
        print(
            json.dumps({"clusters": clusters, "window_hours": hours, "threshold": threshold}, sort_keys=True)
        )
        return 0
    except Exception:  # noqa: BLE001 — Rationale: top-level cron; log-and-exit-nonzero
        log.exception("incident_clustering: unhandled error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
