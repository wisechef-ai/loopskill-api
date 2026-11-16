#!/usr/bin/env python3
"""LoopSkill sync-report collector — the agent-side telemetry drain.

activate_0701 Phase T client half, packaged for a stranger's host (converge_0208
P4), plus the scheduler-derived loop producer t_de052498 added. Collects
everything accumulated since the last cycle and POSTs it as ONE batched
``POST /api/sync-report``:

  - ``loop_runs``     — TWO producers, one surface:
      * spool files written by ``loopskill-emit-run.sh`` (what the AGENT chose
        to report: cost, duration, whether it landed a change)
      * scheduler-derived records for every new fire of a platform-managed
        ``loopskill/*`` cron, read out of the host scheduler's own record
        (``last_run_at`` + ``last_status`` + the placement epoch loop_apply
        stamped). The agent cannot decline to report this one, which is the
        point: a prompt-driven emitter that a crashed loop never reached is
        indistinguishable from a loop that never ran.
  - ``cron_health``   — failures + counts parsed from the host scheduler
  - ``lockfile_state``— what is actually installed on this agent right now

Sent spool files move to ``outbox/.sent/`` and scheduler fire-watermarks advance
in ``state/loop-run-watermarks.json`` — both only after a successful POST
(at-least-once semantics: a POST that succeeds but whose response is lost
re-sends next cycle rather than dropping the record, and the server's run
registry dedups the duplicate on ``(loop, tick, attempt, epoch)``).

**Always exits 0.** This runs from cron next to real work; a telemetry drain that
returns non-zero flips the host's own watchdog and pages a human about nothing.
Errors print to stdout for the cron log and retry on the next cycle.

Member key resolution, in order (the cycle log prints which one won —
``key_source=…`` — because a chain whose fallbacks are invisible is how
``LOOPSKILL_MEMBER_KEY_FILE`` sat ignored in a crontab for four months):
  1. ``$LOOPSKILL_MEMBER_KEY``
  2. ``$RECIPES_API_KEY``
  3. ``$LOOPSKILL_MEMBER_KEY_FILE`` / ``~/.hermes/loopskill/member.key`` — the
     0600 file ``install-loop-apply.sh`` writes
  4. ``$LOOPSKILL_SECRETS`` / ``~/.hermes/secrets/loopskill_tori.json`` —
     ``member_api_key_plain_b64``

Paths are overridable by environment so this works on hosts that are not Hermes:
``LOOPSKILL_API``, ``LOOPSKILL_OUTBOX``, ``LOOPSKILL_CRON_STATE``,
``LOOPSKILL_LOCKFILE``, ``LOOPSKILL_SKILLS_DIR``, ``LOOPSKILL_STATE_DIR`` (where
the scheduler-fire watermarks live), ``LOOPSKILL_INSTANCE`` (the ``instance_key``
stamped on every record this collector produces).
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

API = os.environ.get("LOOPSKILL_API", "https://app.loopskill.io")
HERMES = Path.home() / ".hermes"

SECRETS = Path(os.environ.get("LOOPSKILL_SECRETS", HERMES / "secrets" / "loopskill_tori.json"))
KEY_FILE = Path(os.environ.get("LOOPSKILL_MEMBER_KEY_FILE", HERMES / "loopskill" / "member.key"))
OUTBOX = Path(os.environ.get("LOOPSKILL_OUTBOX", HERMES / "loopskill" / "outbox"))
SENT = OUTBOX / ".sent"
CRON_STATE = Path(os.environ.get("LOOPSKILL_CRON_STATE", HERMES / "cron" / "jobs.json"))
LOCKFILE = Path(os.environ.get("LOOPSKILL_LOCKFILE", HERMES / "loopskill" / "state" / "loopskill-lock.json"))
SKILLS_DIR = Path(os.environ.get("LOOPSKILL_SKILLS_DIR", HERMES / "skills"))
STATE_DIR = Path(os.environ.get("LOOPSKILL_STATE_DIR", HERMES / "loopskill" / "state"))
WATERMARKS = STATE_DIR / "loop-run-watermarks.json"

# Server-side caps (app/services/sync_report.py) — respected client-side so a
# backlog is drained in bounded batches instead of being silently truncated.
MAX_LOOP_RUNS = 200
MAX_LOCKFILE_SKILLS = 500

# ── managed-job vocabulary (must match app/loop_apply.py) ────────────────────
# loop_apply owns every job it writes: namespaced ``loopskill/<loop_id>`` and
# stamped ``{"loopskill": {"loop_id", "epoch", "applied_at"}}``. Nothing outside
# that namespace is ours to report — a host's own crons are invisible to
# loop_apply by the same rule, and reporting them as loop runs would be this
# collector inventing a fact the platform never placed.
MANAGED_PREFIX = "loopskill/"

# The scheduler's recorded status → the registry's outcome vocabulary. Anything
# else the scheduler holds is reported as ``unknown``, never as a silent
# success: app/services/run_registry.py makes ``unknown`` a first-class outcome
# precisely so "we could not read the terminal state" cannot read as "it
# worked". Same reasoning as a missing last_status on a job that clearly fired.
_SCHED_STATUS_TO_OUTCOME = {"ok": "pass", "error": "fail"}

# Watermark file bound. One key per (loop, fire) currently known; a host with
# hundreds of loops over months would otherwise grow it forever.
MAX_WATERMARK_KEYS = 2000

# ``2026-11-16T06:35:39.000267+00:00`` → ``2026-11-16T06:35:39+00:00``. Only the
# fractional seconds: they are wall-clock noise that must not make one firing
# look like two ticks.
_FRACTION_RE = re.compile(r"\.\d+")


def resolve_member_key() -> tuple[str | None, str]:
    """Return ``(key, source)`` — the source is not decoration.

    t_de052498: the deployed collector on wisechef-agents (Jul 6) read only
    ``~/.hermes/secrets/loopskill_member.json`` while its own crontab line
    passed ``LOOPSKILL_MEMBER_KEY_FILE=…``. It authenticated fine — off the
    *other* file — so the ignored variable was invisible for four months. The
    failure mode of a resolution chain is not a crash, it is a silent fallback
    to whichever source happens to exist, so the chain now names the one it
    used in the cron log.
    """
    for var in ("LOOPSKILL_MEMBER_KEY", "RECIPES_API_KEY"):
        val = os.environ.get(var)
        if val:
            return val, f"env:{var}"
    try:
        key = KEY_FILE.read_text().strip()
        if key:
            return key, f"file:{KEY_FILE}"
    except OSError:
        pass
    try:
        d = json.loads(SECRETS.read_text())
        b64 = d.get("member_api_key_plain_b64")
        if b64:
            return base64.b64decode(b64).decode(), f"secrets:{SECRETS}"
    except (OSError, ValueError, KeyError):
        pass
    return None, "none"


def member_key() -> str | None:
    """Resolve the FLEET MEMBER key. Loop telemetry is a member surface."""
    return resolve_member_key()[0]


def collect_loop_runs() -> tuple[list[dict], list[Path]]:
    """Read the spool `loopskill-emit-run.sh` writes. Unparseable files are
    left in place rather than dropped — a partial write completes next cycle."""
    runs: list[dict] = []
    files: list[Path] = []
    if not OUTBOX.is_dir():
        return runs, files
    for f in sorted(OUTBOX.glob("*.json"))[:MAX_LOOP_RUNS]:
        try:
            runs.append(json.loads(f.read_text()))
            files.append(f)
        except (OSError, ValueError):
            continue
    return runs, files


def _instance_key() -> str:
    """Same convention ``loopskill-emit-run.sh`` uses, so both producers of a
    ``LoopRun`` row describe the same host identically."""
    env = os.environ.get("LOOPSKILL_INSTANCE")
    if env:
        return env[:255]
    host = socket.gethostname() or "unknown"
    return f"{host.split('.')[0]}/default"[:255]


def _load_watermarks() -> dict[str, str]:
    try:
        raw = json.loads(WATERMARKS.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_watermarks(watermarks: dict[str, str]) -> None:
    """Persist the reported-fire watermarks atomically.

    A watermark we cannot write is not a lost record: the next cycle re-sends
    and the server dedups on ``(loop, tick, attempt, epoch)``, so the failure
    mode is a duplicate the registry refuses, not a hole in the history. That
    is the whole reason the client may be sloppy here and the server may not.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        bounded = watermarks
        if len(bounded) > MAX_WATERMARK_KEYS:
            bounded = dict(sorted(bounded.items())[-MAX_WATERMARK_KEYS:])
        tmp = WATERMARKS.with_name(WATERMARKS.name + ".tmp")
        tmp.write_text(json.dumps(bounded, sort_keys=True))
        tmp.replace(WATERMARKS)
    except OSError:
        pass  # Rationale: watchdog pattern — telemetry bookkeeping must never fail its cron.


def collect_scheduled_loop_runs() -> tuple[list[dict], dict[str, str]]:
    """Turn each NEW fire of a platform-managed loop into a run record.

    This is the producer that ``loop_runs`` never had. 20,828 of its 20,829
    rows were one beacon whose entire prompt was an emitter call, and the one
    real row was a hand-written Phase-T test — because the only way a loop ever
    reported was a line inside its own prompt, and no loop author wrote one
    (``scripts/audit_public_surface.py`` had a rule pinned to the claim "loop
    telemetry is NOT automatic").

    So the collector reads the SCHEDULER's record instead. The pair
    ``(last_run_at, last_status)`` is written once per completed fire, together,
    under the jobs lock — ``mark_job_run`` → ``_record_run_outcome`` in
    ``cron/jobs.py`` — while ``claim_job_for_fire`` stamps only ``fire_claim``
    and ``next_run_at``. A fire in flight therefore cannot present a new
    ``last_run_at`` carrying its predecessor's status: there is no torn pair to
    mis-report as a pass.

    Returns ``(records, pending_watermarks)``. The caller advances watermarks
    ONLY for records that actually made it into a delivered batch — at-least-
    once, same contract as the outbox spool.

    Honest limits, stated because a silent one would be a lie:
      * ``accepted_change`` is always False. The scheduler cannot observe
        whether a run landed a change; only the agent can, via the emitter.
      * ``attempt`` is always 0. Hermes re-runs a model-unreachable fire
        without a retry counter, and both attempts share one scheduled boundary,
        so the registry's dedup keeps the FIRST terminal record. A retry that
        recovered a failure therefore still reads as a failure — which errs
        toward under-claiming success, the only direction worth erring in.
      * A status the scheduler recorded that is neither ``ok`` nor ``error``
        (``delivery_failed``, ``blocked_config``, a NULL) is ``unknown``, never
        a pass.
    """
    try:
        jobs = json.loads(CRON_STATE.read_text())
    except (OSError, ValueError):
        return [], {}
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs", [])
    if not isinstance(jobs, list):
        return [], {}

    seen = _load_watermarks()
    records: list[dict] = []
    pending: dict[str, str] = {}

    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "")
        if not name.startswith(MANAGED_PREFIX):
            continue  # not a loop the platform placed — a host's own cron is not our telemetry
        meta = job.get("loopskill")
        meta = meta if isinstance(meta, dict) else {}
        loop_id = str(meta.get("loop_id") or name[len(MANAGED_PREFIX) :])[:255]
        last_run_at = job.get("last_run_at")
        if not last_run_at:
            continue  # never fired — there is no fact to report, and none is invented
        fire_key = f"{loop_id}|{last_run_at}"
        if seen.get(loop_id) == fire_key:
            continue  # already reported; re-sending it is the server's problem, not the cron log's

        status = job.get("last_status")
        outcome = _SCHED_STATUS_TO_OUTCOME.get(str(status), "unknown")

        # tick_id is the LOGICAL firing, not the moment we happened to read it:
        # prefer the scheduler's own scheduled boundary so a late read, a cron
        # retry, or a second member all agree on which occurrence they describe.
        # Only the microsecond field is stripped — the UTC offset is kept, so two
        # renderings of the same instant still collide and two different
        # instants never falsely do.
        dispatch = job.get("last_dispatch")
        boundary = None
        if isinstance(dispatch, dict) and dispatch.get("scheduled_at"):
            boundary = dispatch["scheduled_at"]
        tick_id = f"{loop_id}@{_FRACTION_RE.sub('', str(boundary or last_run_at))}"[:255]

        detail = f"scheduler: status={status}"
        if isinstance(dispatch, dict) and dispatch.get("kind"):
            detail += f" dispatch={dispatch['kind']}"
        if job.get("last_error"):
            detail += f" error={str(job['last_error'])[:400]}"

        epoch = meta.get("epoch")
        records.append(
            {
                "loop_slug": loop_id,
                "instance_key": _instance_key(),
                "outcome": outcome,
                "accepted_change": False,
                "cost_usd": None,
                "duration_seconds": None,
                "started_at": str(last_run_at),
                "detail": detail[:2000],
                "tick_id": tick_id,
                "attempt": 0,
                "placement_epoch": epoch if isinstance(epoch, int) and not isinstance(epoch, bool) else None,
            }
        )
        pending[loop_id] = fire_key

    return records, pending


def collect_cron_health() -> dict | None:
    try:
        jobs = json.loads(CRON_STATE.read_text())
    except (OSError, ValueError):
        return None
    if isinstance(jobs, dict):
        jobs = jobs.get("jobs", [])
    if not isinstance(jobs, list):
        return None
    failed, ok = [], 0
    for j in jobs:
        if not isinstance(j, dict):
            continue
        if j.get("last_status") == "error":
            failed.append(
                {
                    "job_name": str(j.get("name") or j.get("id", "?"))[:100],
                    "last_status": "error",
                    "consecutive_failures": int(j.get("consecutive_failures") or 1),
                }
            )
        else:
            ok += 1
    return {"failed": failed[:50], "counts": {"total": len(jobs), "ok": ok, "error": len(failed)}}


def lockfile_state() -> list[dict]:
    """Full installed-state picture: loopskill-managed skills (from the lockfile,
    versioned + checksummed) PLUS the agent's local skill library (hand-built
    skills never deployed through LoopSkill). The local ones are the HARVEST
    CANDIDATES the fleet console surfaces ("built on this agent, in no bundle —
    promote it?"). Lockfile wins on slug collision."""
    entries: dict[str, dict] = {}
    try:
        lock = json.loads(LOCKFILE.read_text())
        for s in lock.get("skills", []):
            entries[s["slug"]] = {
                "slug": s["slug"],
                "pinned_version": s.get("pinned_version"),
                "sha256": s.get("sha256"),
            }
    except (OSError, ValueError, KeyError):
        pass
    if SKILLS_DIR.is_dir():
        try:
            found = sorted(SKILLS_DIR.glob("*/SKILL.md")) + sorted(SKILLS_DIR.glob("*/*/SKILL.md"))
            for md in found:
                slug = md.parent.name
                if slug not in entries:
                    entries[slug] = {"slug": slug, "pinned_version": None, "sha256": None}
        except OSError:
            pass
    return list(entries.values())[:MAX_LOCKFILE_SKILLS]


def build_payload(extra_loop_runs: list[dict] | None = None) -> tuple[dict, list[Path], dict | None]:
    """Assemble one batched cycle.

    ``extra_loop_runs`` are the scheduler-derived records from
    :func:`collect_scheduled_loop_runs`. Spool records go FIRST on purpose: if
    a host has both, the author-reported ones carry the fields a scheduler
    cannot know (cost, duration, accepted_change), and the server caps the
    batch at ``MAX_LOOP_RUNS``. Dropping scheduler-derived records costs nothing
    permanent — their watermark does not advance, so they return next cycle.
    """
    runs, run_files = collect_loop_runs()
    runs = runs + list(extra_loop_runs or [])
    cron = collect_cron_health()
    payload: dict = {"cycle_ts": datetime.now(UTC).isoformat()}
    state = lockfile_state()
    if state:
        payload["lockfile_state"] = state
    if runs:
        payload["loop_runs"] = runs[:MAX_LOOP_RUNS]
    if cron is not None:
        payload["cron_health"] = cron
    return payload, run_files, cron


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # ``--dry-run`` prints what this host WOULD claim and sends nothing — no
    # POST, no spool files moved, no watermark advanced. A stranger needs to see
    # that before their host starts claiming it, and "trust the cron log
    # afterwards" is how this feature sat unmeasured for a year.
    dry_run = "--dry-run" in argv

    key, key_source = resolve_member_key()
    scheduled, pending = collect_scheduled_loop_runs()
    payload, run_files, cron = build_payload(extra_loop_runs=scheduled)

    if dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        print(
            f"sync-report: DRY RUN — nothing posted "
            f"[{len(payload.get('loop_runs') or [])} loop_runs, "
            f"{len(scheduled)} scheduler-derived, key_source={key_source}]"
        )
        return 0

    if not key:
        print(
            "sync-report: no member key (set LOOPSKILL_MEMBER_KEY or enroll first) — skipping [key_source=none]"
        )
        return 0

    req = urllib.request.Request(
        f"{API.rstrip('/')}/api/sync-report",
        data=json.dumps(payload).encode(),
        headers={"x-api-key": key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310  # Rationale: fixed https API base, member-key auth.
            result = json.loads(resp.read().decode())
    # Rationale: watchdog pattern — a telemetry drain must never fail its cron.
    except Exception as exc:  # noqa: BLE001
        print(f"sync-report POST failed (retry next cycle): {exc}")
        return 0

    recorded = result.get("recorded", {})
    if run_files:
        SENT.mkdir(parents=True, exist_ok=True)
        for f in run_files:
            try:
                f.rename(SENT / f.name)
            except OSError:
                pass

    # Advance watermarks for DELIVERED ticks only. Which ticks made the batch is
    # read back out of the payload rather than assumed, because build_payload
    # may have truncated the list at MAX_LOOP_RUNS — advancing a watermark for a
    # record that never left the host would delete a fact from history.
    if pending:
        delivered = {
            str(r.get("tick_id")).rsplit("@", 1)[0]
            for r in (payload.get("loop_runs") or [])
            if isinstance(r, dict) and r.get("tick_id")
        }
        advanced = {lid: fire for lid, fire in pending.items() if lid in delivered}
        if advanced:
            merged = _load_watermarks()
            merged.update(advanced)
            _write_watermarks(merged)

    n_runs = recorded.get("loop_runs", 0)
    if n_runs or scheduled or (cron and cron["counts"]["error"]):
        parts = [f"{n_runs} loop_runs"]
        if scheduled:
            # The dedup counters come back from the server: "we sent N" is not
            # the same claim as "the registry recorded N", and the cron log is
            # the only place a reader ever sees the difference.
            parts.append(
                f"{len(scheduled)} scheduler-derived"
                f" (registry_new={recorded.get('loop_runs_registry_new', '?')}"
                f" deduped={recorded.get('loop_runs_registry_deduped', '?')})"
            )
        if cron and cron["counts"]["error"]:
            parts.append(f"cron errors={cron['counts']['error']}")
        parts.append(f"key_source={key_source}")
        print("sync-report: " + ", ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
