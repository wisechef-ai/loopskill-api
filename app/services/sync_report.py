"""Sync-report ingestion service — activate_0701 Phase T.

Batched ingestion of fleet telemetry (loop runs, cron health, lockfile state).
Kept in the services layer so the route module stays thin (600-line gate).

RETIRED (t_a7ebcc96, 2026-11-16): the `skill_errors` section is no longer
ingested. It was the ONLY writer of skill_error_reports, and the fleet client
never sent the key — scripts/loopskill-collect-reports.py:build_payload() builds
cycle_ts / lockfile_state / loop_runs / cron_health and nothing else.

The emptiness was structural, not an outage. This endpoint stayed demonstrably
healthy the whole time: 22,834 accepted sync cycles between 2026-07-02 and
2026-11-16 (~167/day across the 3 live members; count of cron_health_snapshots,
prod, measured 2026-11-16 — the newest row landed 7 minutes before that
measurement), and skill_error_reports still held 0 rows. A field no client
produces cannot fill a table no matter how healthy the ingest is.

The request field is still ACCEPTED (ignored) for wire compatibility; see
app/sync_report_routes.py. Skill errors agents actually report are IncidentReport
rows — POST /api/v1/skill-error, POST /api/feedback/incident, MCP
loopskill_report_skill_error.

Key functions:
  - ingest_sync_report: caps, truncation, inserts. Returns (recorded, truncated).
    Entries carrying a ``tick_id`` are routed through the Phase-D run registry
    (app/services/run_registry.py) instead of being inserted blind — see
    t_de052498 below.
  - rollup_loop_runs: idempotent daily UPSERT from raw LoopRun rows.
  - prune_raw: deletes LoopRun + CronHealthSnapshot past retention (30d).
  - cost_per_accepted_change: query helper reading rollups.

RETIRED? NO — t_de052498 (2026-11-16) chose the opposite of the
``skill_error_reports`` disposition, and the reason is worth keeping next to
the function that benefited from it. ``loop_runs`` held 20,828 rows of one
self-originated beacon and exactly ONE real run (a hand-written Phase-T test),
for the same structural reason: nothing produced it. But here the gap was a
caller, not a feature — ``scripts/loopskill-emit-run.sh`` was shipped, hardened
and tested, and no scheduler ever called it, because the member-side apply
(``app/loop_apply_cli.py``) was written with "follow-up wiring on the member
host" in its own docstring and that follow-up never happened. Zero
``loopskill/*`` jobs existed in ``~/.hermes/cron/jobs.json`` on any fleet host
(measured 2026-11-16), so the three ``active`` rows in ``loop_placements``
described crons that had never existed.

The producer now reads the SCHEDULER's record of a fire rather than trusting a
line inside the loop's own prompt: ``collect_scheduled_loop_runs()`` in the
collector turns each new ``loopskill/*`` fire into a tick-bearing record. A
prompt-driven emitter can only report what the agent decides to say, and a
crashed loop says nothing — which is indistinguishable from "never scheduled",
the exact FAILING-vs-OVERDUE ambiguity app/services/loop_convergence.py exists
to resolve.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    CronHealthSnapshot,
    FleetMember,
    LoopRun,
    LoopRunDailyRollup,
    MemberLockfileSnapshot,
)
from app.services.run_registry import (
    OUTCOME_FAIL,
    OUTCOME_PASS,
    OUTCOME_UNKNOWN,
    ingest_run,
)
from app.services.synthetic_runs import (
    SELF_ORIGINATED_LOOP_SLUGS,
    RunCounts,
    origin_verdict_for_member,
)

logger = logging.getLogger(__name__)

# ── one vocabulary, on the way in ────────────────────────────────────────────
# ``loop_runs`` carries TWO outcome vocabularies today and both are legitimate:
# the legacy at-least-once emitter says ``success``/``failure``
# (``scripts/loopskill-emit-run.sh``, docs/SELF_HOST.md), the Phase-D registry
# says ``pass``/``fail``/``unknown``. app/services/loop_convergence.py already
# has to accept both (``_PASS_OUTCOMES = {"pass", "success"}``) precisely
# because nothing ever reconciled them at the boundary.
#
# A record that arrives on the HONEST path (it carries a ``tick_id``) is
# normalized here, once, before :func:`ingest_run` — which is stricter than the
# legacy path and would otherwise file ``success`` as ``unknown``, silently
# removing every real run from the pass-rate denominator. A number that quietly
# stops counting is worse than a number that is wrong.
_OUTCOME_ALIASES = {
    "success": OUTCOME_PASS,
    "pass": OUTCOME_PASS,
    "ok": OUTCOME_PASS,
    "failure": OUTCOME_FAIL,
    "fail": OUTCOME_FAIL,
    "error": OUTCOME_FAIL,
    # A loop killed by its budget or turn cap terminated KNOWN and did NOT
    # pass. Mapping these to ``unknown`` would drop them out of the denominator
    # entirely — which is exactly how a loop that never finishes reads as
    # healthy. They are counted as not-passed, and they are never a pass.
    "budget_stop": OUTCOME_FAIL,
    "max_turns_stop": OUTCOME_FAIL,
}


def _to_int(value: Any) -> int | None:
    """Coerce a client-supplied integer, rejecting bools and junk.

    ``member_seq``/``attempt``/``placement_epoch`` are dedup and ordering axes;
    ``True`` arriving as ``1`` would collide a real attempt, and a string would
    raise deep inside the insert. Anything un-coercible is ``None`` — absent,
    not zero.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    """Coerce a client-supplied cost, same rationale as :func:`_to_int`."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── D9 size caps (from design contract §endpoint) ──────────────────────────

MAX_LOOP_RUNS = 200
MAX_CRON_FAILED = 50
MAX_FIELD_LEN = 2000
MAX_LOCKFILE_SKILLS = 500  # feat/fleet-console-state — cap per snapshot

# Server-side body cap (checked in the route handler before JSON parse).
MAX_BODY_BYTES = 256 * 1024  # 256 KB

# Retention window for raw telemetry rows.
DEFAULT_RETENTION_DAYS = 30


def _truncate(value: str | None, limit: int = MAX_FIELD_LEN) -> str | None:
    """Truncate a string to ``limit`` characters, preserving None."""
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit]


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime.

    Returns None for None/empty input. Tolerant of trailing 'Z' and
    offset-naive strings (assumes UTC).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Normalize trailing Z to +00:00 for fromisoformat compatibility.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def ingest_sync_report(
    db: Session,
    member: FleetMember,
    payload: dict[str, Any],
) -> tuple[dict[str, int | bool], dict[str, int]]:
    """Ingest one batched sync-report payload.

    Applies caps + truncation, inserts rows, bumps FleetMember.updated_at
    as the liveness marker. lockfile_state is NOT stored as rows (D9).

    Returns:
        (recorded, truncated) where recorded has the counts and truncated
        describes any dropped excess items.
    """
    now = datetime.now(UTC)
    truncated: dict[str, int] = {}

    # ── loop_runs ───────────────────────────────────────────────────────────
    loop_runs = payload.get("loop_runs") or []
    lr_truncated = 0
    if len(loop_runs) > MAX_LOOP_RUNS:
        lr_truncated = len(loop_runs) - MAX_LOOP_RUNS
        loop_runs = loop_runs[:MAX_LOOP_RUNS]
    if lr_truncated:
        truncated["loop_runs"] = lr_truncated

    # mesh_0408 W4: resolve the IDENTITY's verdict ONCE per report (the
    # member/fleet/key markers are constant across the batch) and fall back to
    # the per-slug backstop only when nobody has classified this identity.
    # Frozen onto each row so counts never need a join.
    #
    # W4b: this is a precedence, NOT an OR. Or-ing the slug in unconditionally
    # meant an explicitly-external customer who declared `p4-loop-proof` was
    # counted as our own beacon — the slug list overruling a real verdict is
    # the bug the backstop is supposed to be a backstop against.
    member_verdict = origin_verdict_for_member(db, member)
    synthetic_ingested = 0
    registry_new = 0
    registry_deduped = 0
    legacy_inserted = 0
    malformed = 0

    # TWO PASSES, deliberately. ``ingest_run`` is a compare-and-swap and commits
    # per row; running it in the same loop as the legacy ``db.add`` inserts
    # would flush every not-yet-validated legacy row the moment the first
    # tick-bearing record landed.
    #
    # A record is on the honest path iff it carries a ``tick_id``. That is the
    # whole routing rule, and it is why the field is worth having: without it a
    # duplicate delivery is a second fact, and a second fact is a second vote in
    # whatever pass rate someone computes downstream.
    #
    # ``isinstance`` first: a non-dict entry used to reach ``lr.get(...)`` and
    # raise AttributeError, so one malformed element from a fleet agent turned a
    # whole batched telemetry cycle into a 500.
    for lr in loop_runs:
        if not isinstance(lr, dict):
            # Counted, not swallowed: an ack that silently drops entries is how
            # a producer starts believing it is reporting.
            malformed += 1
            continue
        if (lr.get("tick_id") or "")[:255]:
            continue  # honest path — handled in the second pass
        slug = (lr.get("loop_slug") or "")[:255]
        run_synthetic = member_verdict if member_verdict is not None else slug in SELF_ORIGINATED_LOOP_SLUGS
        synthetic_ingested += int(run_synthetic)
        db.add(
            LoopRun(
                member_id=member.id,
                fleet_id=member.fleet_id,
                loop_slug=slug,
                is_synthetic=run_synthetic,
                instance_key=(lr.get("instance_key") or "")[:255],
                outcome=(lr.get("outcome") or "failure")[:32],
                accepted_change=bool(lr.get("accepted_change", False)),
                # Coerced, not passed through: these are Numeric columns, and a
                # fleet agent sending ``cost_usd: "n/a"`` used to raise on flush
                # and 500 the entire batched telemetry cycle.
                cost_usd=_to_float(lr.get("cost_usd")),
                duration_seconds=_to_int(lr.get("duration_seconds")),
                provenance_id=(lr.get("provenance_id") or None)[:64] if lr.get("provenance_id") else None,
                started_at=_parse_dt(lr.get("started_at")),
                detail=_truncate(lr.get("detail")),
            )
        )
        legacy_inserted += 1

    for lr in loop_runs:
        if not isinstance(lr, dict):
            continue
        tick_id = (lr.get("tick_id") or "")[:255]
        if not tick_id:
            continue
        slug = (lr.get("loop_slug") or "")[:255]
        raw_outcome = str(lr.get("outcome") or "").strip().lower()
        res = ingest_run(
            db,
            member_id=member.id,
            fleet_id=member.fleet_id,
            loop_slug=slug,
            tick_id=tick_id,
            attempt=_to_int(lr.get("attempt")) or 0,
            placement_epoch=_to_int(lr.get("placement_epoch")),
            outcome=_OUTCOME_ALIASES.get(raw_outcome, OUTCOME_UNKNOWN),
            member_seq=_to_int(lr.get("member_seq")),
            cost_usd=_to_float(lr.get("cost_usd")),
            duration_seconds=_to_int(lr.get("duration_seconds")),
            detail=_truncate(lr.get("detail")),
        )
        if res.deduped:
            registry_deduped += 1
        else:
            registry_new += 1
            # The row itself is stamped inside ingest_run by
            # classify_run_synthetic(member_id=...); this is the same verdict
            # from the same inputs, counted here so the ack's split still
            # covers registry rows. Two derivations of one rule is the seam to
            # watch if either side ever diverges.
            synthetic_ingested += int(
                member_verdict if member_verdict is not None else slug in SELF_ORIGINATED_LOOP_SLUGS
            )

    # ── skill_errors ─ RETIRED (t_a7ebcc96, 2026-11-16) ─────────────────────
    # This block was the sole writer of skill_error_reports. It read
    # payload["skill_errors"], a key no client has ever produced; the table held
    # 0 rows across the feature's entire life while this endpoint ingested
    # 22,834 accepted sync cycles.

    # ── cron_health ──────────────────────────────────────────────────────────
    cron_health = payload.get("cron_health")
    cron_stored = False
    if cron_health and isinstance(cron_health, dict):
        failed_list = cron_health.get("failed") or []
        ch_truncated = 0
        if len(failed_list) > MAX_CRON_FAILED:
            ch_truncated = len(failed_list) - MAX_CRON_FAILED
            failed_list = failed_list[:MAX_CRON_FAILED]
        if ch_truncated:
            truncated["cron_health_failed"] = ch_truncated

        counts = cron_health.get("counts") or {}
        db.add(
            CronHealthSnapshot(
                member_id=member.id,
                fleet_id=member.fleet_id,
                failed=failed_list,
                total_count=int(counts.get("total", 0)),
                ok_count=int(counts.get("ok", 0)),
                error_count=int(counts.get("error", 0)),
            )
        )
        cron_stored = True

    # ── lockfile_state (feat/fleet-console-state) ────────────────────────────
    # ONE latest-state row per member, upserted — O(fleet size) not O(time),
    # so this stays within the D9 data-efficiency posture. Answers "what is
    # actually installed on this agent right now" for the fleet console.
    lockfile_state = payload.get("lockfile_state")
    lockfile_stored = False
    if isinstance(lockfile_state, list):
        capped = lockfile_state[:MAX_LOCKFILE_SKILLS]
        if len(lockfile_state) > MAX_LOCKFILE_SKILLS:
            truncated["lockfile_state"] = len(lockfile_state) - MAX_LOCKFILE_SKILLS
        clean = [
            {
                "slug": (s.get("slug") or "")[:255],
                "pinned_version": (s.get("pinned_version") or None),
                # Agents' collector scripts ship the checksum as "sha256";
                # the raw lockfile field is "checksum_sha256". Accept both.
                "checksum_sha256": (s.get("checksum_sha256") or s.get("sha256") or None),
            }
            for s in capped
            if isinstance(s, dict) and s.get("slug")
        ]
        snap = db.query(MemberLockfileSnapshot).filter(MemberLockfileSnapshot.member_id == member.id).first()
        if snap is None:
            db.add(
                MemberLockfileSnapshot(
                    member_id=member.id,
                    fleet_id=member.fleet_id,
                    skills=clean,
                    cycle_ts=(payload.get("cycle_ts") or None),
                )
            )
        else:
            snap.skills = clean
            snap.cycle_ts = payload.get("cycle_ts") or None
            snap.reported_at = now
        lockfile_stored = True

    # Bump FleetMember.updated_at as the liveness marker (drift computation
    # stays in the reconcile endpoint; the snapshot above is the console's
    # actual-state read surface).
    member.updated_at = now

    db.commit()

    # mesh_0408 W4: the ack reports a run count, so it reports the split too —
    # ``loop_runs`` alone would be the same combined-figure-only lie the
    # dashboard used to tell.
    # ``total`` counts rows this call actually wrote — NOT ``len(loop_runs)``.
    # With two producers the entry count and the written count are different
    # numbers: a re-delivered tick writes nothing, and a malformed entry writes
    # nothing, and an ack that reports arrivals as recordings is the exact
    # unfalsifiable-number defect t_a7ebcc96 retired a whole table over.
    ingested = RunCounts(total=legacy_inserted + registry_new, synthetic=synthetic_ingested)
    # t_a7ebcc96: skill_errors is no longer persisted, but the ack still counts
    # what arrived, under a key that says so. The old ack reported
    # ``"skill_errors": N`` from a table that never held a row — an unfalsifiable
    # number that read as health. ``skill_errors_ignored`` makes the dead rail
    # observable instead: a client that starts sending the field sees its own
    # payload counted and dropped, rather than a silent success.
    _se_arrived = payload.get("skill_errors")
    recorded: dict[str, int | bool] = {
        "loop_runs": ingested.total,
        "loop_runs_synthetic": ingested.synthetic,
        "loop_runs_external": ingested.external,
        # t_de052498: the honest path reports what its dedup actually did. A
        # client that re-sends a tick it already delivered must be able to see
        # the server refuse the second copy — an ack that counts arrivals only
        # cannot tell "recorded" from "recorded twice".
        "loop_runs_registry_new": registry_new,
        "loop_runs_registry_deduped": registry_deduped,
        "loop_runs_malformed": malformed,
        "skill_errors_ignored": len(_se_arrived) if isinstance(_se_arrived, list) else 0,
        "cron_health": cron_stored,
        "lockfile_state": lockfile_stored,
    }
    return recorded, truncated


def utc_today() -> date:
    """The current day on the rollup's clock.

    Rollup buckets are UTC days (``day_start`` below is midnight UTC), so the
    default day must be read on the SAME clock. ``date.today()`` is the host's
    local date: on a non-UTC host it names a different day for up to the UTC
    offset's worth of hours each night (00:00-02:00 under CEST), and a default
    rollup then aggregates a window that holds none of the day's runs.
    """
    return datetime.now(UTC).date()


def rollup_loop_runs(db: Session, day: date | None = None) -> int:
    """Aggregate all LoopRun rows for ``day`` into LoopRunDailyRollup.

    Idempotent: re-running for the same day produces the same aggregates.
    Uses delete-then-insert in SQLite (test path); the unique constraint
    ensures correctness on Postgres via ON CONFLICT when available.

    Returns the number of rollup rows written.
    """
    if day is None:
        day = utc_today()

    # Find all distinct (fleet_id, member_id, loop_slug) groups for this day.
    # We compute the date from LoopRun.created_at (timezone-aware).
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)

    # Delete existing rollups for this day so re-insertion is idempotent.
    db.query(LoopRunDailyRollup).filter(LoopRunDailyRollup.day == day).delete()

    # Aggregate from raw rows — load grouped rows, then iterate to compute
    # all aggregates per (fleet, member, loop_slug).
    groups = (
        db.query(LoopRun.fleet_id, LoopRun.member_id, LoopRun.loop_slug)
        .filter(LoopRun.created_at >= day_start, LoopRun.created_at < day_end)
        .group_by(LoopRun.fleet_id, LoopRun.member_id, LoopRun.loop_slug)
        .all()
    )

    count = 0
    for fleet_id, member_id, loop_slug in groups:
        # Compute successes/failures/cost/duration in a second pass for this group.
        group_rows = (
            db.query(LoopRun)
            .filter(
                LoopRun.fleet_id == fleet_id,
                LoopRun.member_id == member_id,
                LoopRun.loop_slug == loop_slug,
                LoopRun.created_at >= day_start,
                LoopRun.created_at < day_end,
            )
            .all()
        )
        successes = sum(1 for r in group_rows if r.outcome in ("success", "budget_stop", "max_turns_stop"))
        failures = sum(1 for r in group_rows if r.outcome == "failure")
        # mesh_0408 W4: carried onto the rollup because raw rows are pruned at
        # 30d — without it, every adoption number older than a month would
        # silently re-merge our own beacon back into the total.
        synthetic = sum(1 for r in group_rows if r.is_synthetic)
        accepted_changes = sum(1 for r in group_rows if r.accepted_change)
        cost_total = sum(float(r.cost_usd or 0) for r in group_rows)
        duration_total = sum(r.duration_seconds or 0 for r in group_rows)

        db.add(
            LoopRunDailyRollup(
                fleet_id=fleet_id,
                member_id=member_id,
                loop_slug=loop_slug,
                day=day,
                runs=len(group_rows),
                synthetic_runs=synthetic,
                successes=successes,
                failures=failures,
                accepted_changes=accepted_changes,
                cost_usd_total=round(cost_total, 4) if cost_total else None,
                duration_seconds_total=duration_total or None,
            )
        )
        count += 1

    db.commit()
    return count


def prune_raw(db: Session, older_than_days: int = DEFAULT_RETENTION_DAYS) -> dict[str, int]:
    """Delete LoopRun + CronHealthSnapshot rows past retention.

    NEVER touches rollups. Returns counts of deleted rows per table.
    """
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)

    loop_run_count = db.query(LoopRun).filter(LoopRun.created_at < cutoff).delete()
    cron_count = db.query(CronHealthSnapshot).filter(CronHealthSnapshot.created_at < cutoff).delete()
    db.commit()

    logger.info(
        "sync-report prune: deleted %d loop_runs, %d cron_health_snapshots (older than %d days)",
        loop_run_count,
        cron_count,
        older_than_days,
    )
    return {"loop_runs": loop_run_count, "cron_health_snapshots": cron_count}


def cost_per_accepted_change(
    db: Session,
    fleet_id: UUID,
    loop_slug: str | None = None,
    days: int = 30,
) -> float | None:
    """Query helper: cost per accepted change from rollup tables.

    Returns sum(cost_usd_total) / nullif(sum(accepted_changes), 0), or None
    if there are zero accepted changes.
    """
    since = utc_today() - timedelta(days=days)
    q = db.query(
        func.sum(LoopRunDailyRollup.cost_usd_total),
        func.sum(LoopRunDailyRollup.accepted_changes),
    ).filter(
        LoopRunDailyRollup.fleet_id == fleet_id,
        LoopRunDailyRollup.day >= since,
    )
    if loop_slug is not None:
        q = q.filter(LoopRunDailyRollup.loop_slug == loop_slug)

    total_cost, total_accepted = q.first()
    if total_accepted is None or total_accepted == 0:
        return None
    if total_cost is None:
        return 0.0
    return float(total_cost) / float(total_accepted)
