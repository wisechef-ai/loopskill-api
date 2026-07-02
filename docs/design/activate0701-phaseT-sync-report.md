# Phase T — BATCHED SYNC-REPORT INGESTION — design contract (loopskill_activate_0701)

Author: Tori (opus-role contract). Implementer: subagent. Reviewer: codex gpt-5.5.
Branch: loopskill_activate_0701/phaseT — worktree branches OFF phase1 (needs FleetMember).
Locks: #12 (outcome-record data contract), #14 (ONE batched POST per cycle), D4/D7/D9.

## The endpoint — POST /api/sync-report (member-key auth)

Auth: x-api-key MUST resolve to a FleetMember key (resolve_member_for_key from Phase 1).
Non-member key -> 403 {"error":"member_key_required"}. Anonymous -> 401.
Rate: reuse check_reconcile_abuse_ceiling per api_key_id.

Request (ONE payload per 30-min cycle — the agent batches everything since last cycle):
```json
{
  "cycle_ts": "2026-07-02T21:30:00Z",
  "lockfile_state": [{"slug": "...", "pinned_version": "...", "sha256": "..."}],
  "loop_runs": [{
     "loop_slug": "atomic-habits", "instance_key": "tori/default",
     "outcome": "success|failure|budget_stop|max_turns_stop",
     "accepted_change": true, "cost_usd": 0.42, "duration_seconds": 118,
     "provenance_id": null, "started_at": "...", "detail": "<=2000 chars"
  }],
  "skill_errors": [{"slug": "...", "semver": "...", "signature": "...", "summary": "<=2000 chars"}],
  "cron_health": {"failed": [{"job_name": "...", "last_status": "error", "consecutive_failures": 3}],
                   "counts": {"total": 12, "ok": 11, "error": 1}}
}
```
ALL sections optional (empty cycle = heartbeat-shaped no-op, still 200).
D9 size limits: request body cap 256 KB -> 413 {"error":"payload_too_large"}. Server-side truncation
markers: loop_runs max 200/skill_errors max 100/cron_health.failed max 50 per POST — excess dropped,
response carries {"truncated": {"loop_runs": N}}. detail/summary fields TRUNCATED server-side at 2000 chars.
cron_health stores failures + counts ONLY (N2 default — no full job dumps).

Response 200: {"recorded": {"loop_runs": N, "skill_errors": N, "cron_health": true|false}, "truncated": {...}}

## Models (app/models.py)

```python
class LoopRun(Base):
    """Raw loop outcome record (lock #12). Retained 30d, rolled up daily."""
    __tablename__ = "loop_runs"
    id            UUID pk
    member_id     UUID nullable=False index      # who ran it
    fleet_id      UUID nullable=False index      # denorm for pane queries
    loop_slug     String(255) nullable=False index   # slug not FK: loop may be private/unpublished
    instance_key  String(255) nullable=False
    outcome       String(32) nullable=False      # success|failure|budget_stop|max_turns_stop
    accepted_change Boolean nullable=False server_default=false
    cost_usd      Numeric(10,4) nullable=True
    duration_seconds Integer nullable=True
    provenance_id String(64) nullable=True index
    started_at    DateTime(tz) nullable=True
    detail        Text nullable=True             # truncated 2000
    created_at    DateTime(tz) server_default=now() index
    Index("ix_loop_runs_member_slug_created", member_id, loop_slug, created_at)

class CronHealthSnapshot(Base):
    """Per-member per-cycle cron health (D7). failures+counts only. 30d retention."""
    __tablename__ = "cron_health_snapshots"
    id UUID pk; member_id UUID nullable=False index; fleet_id UUID nullable=False index
    failed JSON nullable=False default=list      # [{job_name,last_status,consecutive_failures}]
    total_count Integer nullable=False; ok_count Integer nullable=False; error_count Integer nullable=False
    created_at DateTime(tz) server_default=now() index

class SkillErrorReport(Base):
    """Agent-reported skill error from the sync cycle (voice pre-wiring for FB phase)."""
    __tablename__ = "skill_error_reports"
    id UUID pk; member_id UUID index; fleet_id UUID index
    slug String(255) nullable=False index; semver String(32) nullable=True
    signature Text nullable=False; summary Text nullable=False   # truncated 2000
    feedback_status Text default="pending"       # FB phase consumes pending rows
    created_at DateTime(tz) server_default=now() index

class LoopRunDailyRollup(Base):
    """Daily rollup per (fleet, member, loop_slug, day) — retained indefinitely (§B.1)."""
    __tablename__ = "loop_run_daily_rollups"
    id UUID pk; fleet_id UUID index; member_id UUID; loop_slug String(255)
    day Date nullable=False
    runs Integer; successes Integer; failures Integer; accepted_changes Integer
    cost_usd_total Numeric(12,4); duration_seconds_total BigInteger
    UniqueConstraint(fleet_id, member_id, loop_slug, day)
```

## Service: app/services/sync_report.py (keep route module thin — 600-line gate)
- ingest_sync_report(db, member, payload) -> caps, truncation, inserts. lockfile_state: NOT stored
  as rows (D9) — bumps FleetMember.updated_at as the liveness marker; drift computation stays in the
  reconcile endpoint (already exists).
- rollup_loop_runs(db, day) -> UPSERT into loop_run_daily_rollups (idempotent, ON CONFLICT DO UPDATE
  aggregates whole day). Postgres upsert; in SQLite tests use merge-equivalent (test path can delete+insert).
- prune_raw(db, older_than_days=30) -> deletes LoopRun + CronHealthSnapshot rows past retention.
  NEVER touches rollups. Returns counts.
- rollup + prune wired as admin endpoints (master-only, cron-callable):
  POST /api/admin/loop-run-rollup {day?}, POST /api/admin/sync-report-prune {days?=30}.
  (Server cron wiring on wisechef-hq = deploy step, systemd timer or existing crons/ pattern — check crons/ dir.)
- cost-per-accepted-change query helper: cost_per_accepted_change(db, fleet_id, loop_slug=None, days=30)
  reading ROLLUPS: sum(cost_usd_total)/nullif(sum(accepted_changes),0).

## Hermes-side emitter (client half — part of this phase)
Extend ~/.hermes/scripts/loopskill-sync.sh (Tori's live cron loopskill-sync-tori):
- New collector script ~/.hermes/scripts/loopskill-collect-reports.py:
  - loop_runs: reads new outcome records since last cycle from a spool dir
    ~/.hermes/loopskill/outbox/*.json (one JSON per run — atomic-habits engine writes these; add a
    writer shim so `atomic_habits_score.py` resolve/ship events drop a spool file; if wiring the
    engine is too invasive this cycle, provide loopskill-emit-run.sh <slug> <outcome> ... as the
    documented producer and use it in the gate test).
  - cron_health: parses `hermes cron list` (or ~/.hermes/cron/jobs state) into failures+counts.
  - POSTs ONE /api/sync-report; on success moves spool files to .sent/; on failure leaves them
    (retry next cycle — at-least-once, server dedupe not required for v1: runs are additive facts).
- sync.sh order: reconcile -> per-skill reconcile-report (existing) -> sync-report batch -> heartbeat.
  Exit 0 always (watchdog pattern preserved).

## Gates (from plan §2 Phase T)
1. One REAL atomic-habits run on Tori lands a complete outcome record (verify row in prod DB).
2. Deliberately-failed cron appears in a CronHealthSnapshot (kill-test).
3. Rollup query returns cost-per-accepted-change.
4. Pruner tested (raw rows past 30d deleted, rollups intact).
5. §B load test green: scripts/load_test_sync_report.py in repo — p95 < 200ms at 10x pro-max rate
   (96,000 POSTs/day equiv — run against LOCAL uvicorn+PG, document VPS-class extrapolation).
   DB size projection documented in docs/design/ (rows/day at 200 agents).

## Tests (tests/test_activate0701_sync_report.py) — RED first
1. member-key POST full payload -> 200, all rows landed with correct member_id/fleet_id.
2. non-member key -> 403; anonymous -> 401.
3. caps: 201 loop_runs -> 200 stored, truncated marker; detail >2000 chars truncated.
4. oversize body -> 413.
5. empty payload -> 200, FleetMember.updated_at bumped.
6. rollup idempotency: run twice for same day -> same aggregate (no doubling).
7. pruner: 31-day-old raw deleted, 29-day kept, rollups untouched.
8. cost-per-accepted-change math (known fixture -> exact value).
9. cron_health failures+counts persisted; failed list capped at 50.
10. version bump contract (0.7.0 -> 0.8.0 in this branch when it merges after Phase 1 — coordinate).
