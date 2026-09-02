"""flywheel_0902/B — the funnel ledger service.

Entity resolution + classification + idempotent event/run recording for the
funnel ledger (funnel_entities/funnel_identifiers/funnel_events/
loop_runs_ledger — see the migration for the full schema rationale).

Council v2 §0.9 corrections this module encodes:

1. TWO ledgers. ``record_event`` writes ONLY real subject-stage transitions;
   ``record_run`` writes ONLY job-execution facts. Never conflate them —
   callers that want "did the flywheel run today" read loop_runs_ledger;
   callers that want "did a stranger move a stage" read funnel_events.
2. Idempotency = the immutable source tuple ``(source_system,
   source_event_id, stage)``. ``record_event`` is a single
   INSERT ... ON CONFLICT DO NOTHING equivalent expressed as a portable
   try/except IntegrityError (works identically on SQLite + Postgres,
   unlike a dialect-specific ON CONFLICT clause) and returns
   ``replay=True`` when the row already existed.
3. Classification (fleet/stranger/unknown) is computed ONCE at ingest and
   persisted with its evidence string — never recomputed at read time. A
   NULL/missing identifier (e.g. install_events.client_ip IS NULL) MUST
   classify as ``unknown``, never ``stranger`` — an unknown-provenance row
   inflating the stranger conversion denominator is exactly the false-green
   case the council caught.

Entity resolution is DELIBERATELY simple in this phase (documented, not
hidden): ``resolve_entity(kind, value)`` is create-or-get against
``funnel_identifiers`` keyed on (kind, value). If two DIFFERENT identifiers
for the SAME real-world subject are seen (e.g. an email and, days later, an
IP for the same person) and no caller ever explicitly links them, they
resolve to TWO DIFFERENT entities. This module does not attempt inference-
based merging — that is M-9 in the design doc, explicitly deferred. Callers
that KNOW two identifiers belong together (e.g. an install_event's
api_key_id joins to a user_id via api_keys.user_id) should resolve BOTH
kinds and pass the SAME resolved entity_id into subsequent record_event
calls when possible, rather than resolving each stage independently.
"""

from __future__ import annotations
from datetime import datetime, timezone

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import yaml
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import FunnelEntity, FunnelEvent, FunnelIdentifier, LoopRunLedger

logger = logging.getLogger(__name__)

IdentifierKind = Literal["email", "handle", "ip", "api_key", "user_id", "stripe_customer"]
Stage = Literal["lead", "contacted", "replied", "signup", "installed", "bundle_created", "paid"]
Classification = Literal["fleet", "stranger", "unknown"]
RunOutcome = Literal["ok", "no_fire", "error"]

_DEFAULT_EXCLUSIONS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "fleet_exclusions.yaml"


def _exclusions_path() -> Path:
    override = (settings.FUNNEL_FLEET_EXCLUSIONS_PATH or "").strip()
    return Path(override) if override else _DEFAULT_EXCLUSIONS_PATH


@lru_cache(maxsize=8)
def _load_fleet_exclusions(path_str: str) -> dict[str, Any]:
    """Load + cache the fleet-exclusion config, keyed by resolved path.

    Cached by path string (not bare @lru_cache()) so tests that point
    FUNNEL_FLEET_EXCLUSIONS_PATH at a different file get a fresh cache
    entry instead of a stale one from a prior test's config.
    """
    path = Path(path_str)
    if not path.exists():
        logger.warning("funnel_ledger: fleet exclusions file not found at %s — treating as empty", path)
        return {"emails": [], "ips": [], "api_key_ids": []}
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    return {
        "emails": {str(e).strip().lower() for e in (data.get("emails") or [])},
        "ips": {str(i).strip() for i in (data.get("ips") or [])},
        "api_key_ids": {str(k).strip() for k in (data.get("api_key_ids") or [])},
    }


def fleet_exclusions() -> dict[str, Any]:
    """Public accessor — the resolved, cached fleet-exclusion sets."""
    return _load_fleet_exclusions(str(_exclusions_path()))


def clear_fleet_exclusions_cache() -> None:
    """Test hook — drop the lru_cache so a rewritten config file is re-read."""
    _load_fleet_exclusions.cache_clear()


# ── Entity resolution ────────────────────────────────────────────────────


def resolve_entity(db: Session, kind: IdentifierKind, value: str) -> UUID:
    """Create-or-get the entity for a single (kind, value) identifier.

    Simple alias-table lookup — see module docstring for the documented
    no-merge-yet limitation. Idempotent: calling this twice with the same
    (kind, value) always returns the same entity_id.
    """
    normalized_value = value.strip()
    existing = db.execute(
        select(FunnelIdentifier).where(
            FunnelIdentifier.kind == kind, FunnelIdentifier.value == normalized_value
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.entity_id

    entity = FunnelEntity(entity_id=uuid4())
    db.add(entity)
    db.flush()

    identifier = FunnelIdentifier(id=uuid4(), entity_id=entity.entity_id, kind=kind, value=normalized_value)
    db.add(identifier)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race against a concurrent resolver for the same (kind,
        # value) — roll back our half-written entity+identifier and read
        # back the winner's row instead of erroring the caller.
        db.rollback()
        winner = db.execute(
            select(FunnelIdentifier).where(
                FunnelIdentifier.kind == kind, FunnelIdentifier.value == normalized_value
            )
        ).scalar_one()
        return winner.entity_id

    return entity.entity_id


# ── Classification ───────────────────────────────────────────────────────


def classify(
    *,
    email: str | None = None,
    ip: str | None = None,
    api_key_id: str | None = None,
) -> tuple[Classification, str]:
    """Classify a subject as fleet / stranger / unknown, with evidence.

    Council v2 §0.9: a NULL/unresolvable identifier (e.g. install_events
    with client_ip IS NULL) must classify as ``unknown``, NEVER ``stranger``
    — an unknown-provenance row can never count toward the stranger
    conversion denominator. ``unknown`` is returned when NONE of the
    supplied identifiers are non-empty (nothing to classify against) or
    when an IP-only classification has no IP.

    Priority: any identifier matching a fleet exclusion wins fleet,
    regardless of the others. Otherwise: if at least one identifier is
    present and non-matching, stranger. If no identifier was supplied at
    all, unknown.
    """
    exclusions = fleet_exclusions()
    had_any_identifier = False

    if email:
        had_any_identifier = True
        normalized = email.strip().lower()
        if normalized in exclusions["emails"]:
            return "fleet", f"email:{normalized} in fleet_exclusions.emails"

    if ip:
        had_any_identifier = True
        if ip in exclusions["ips"]:
            return "fleet", f"ip:{ip} in fleet_exclusions.ips"

    if api_key_id:
        had_any_identifier = True
        if api_key_id in exclusions["api_key_ids"]:
            return "fleet", f"api_key_id:{api_key_id} in fleet_exclusions.api_key_ids"

    if not had_any_identifier:
        return "unknown", "no identifier supplied"

    return "stranger", "no fleet-exclusion match"


# ── Idempotent event recording ───────────────────────────────────────────


def record_event(
    db: Session,
    *,
    stage: Stage,
    entity_id: UUID,
    source_system: str,
    source_event_id: str,
    source_loop: str,
    host: str,
    classification: Classification,
    classification_evidence: str | None = None,
    amount_cents: int | None = None,
    ts: datetime | None = None,
    currency: str | None = None,
    evidence_url: str | None = None,
) -> tuple[FunnelEvent, bool]:
    """Idempotently record one funnel_events row.

    Idempotency key: ``(source_system, source_event_id, stage)`` — see
    module docstring. Returns ``(row, replay)`` where ``replay=True`` means
    the tuple already existed and the EXISTING row is returned unchanged
    (no update-on-conflict — a stage transition happened once, it does not
    get re-dated by a retried writer).
    """
    existing = db.execute(
        select(FunnelEvent).where(
            FunnelEvent.source_system == source_system,
            FunnelEvent.source_event_id == source_event_id,
            FunnelEvent.stage == stage,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, True

    row = FunnelEvent(
        ts=ts or datetime.now(timezone.utc),
        id=uuid4(),
        stage=stage,
        entity_id=entity_id,
        source_system=source_system,
        source_event_id=source_event_id,
        source_loop=source_loop,
        host=host,
        classification=classification,
        classification_evidence=classification_evidence,
        amount_cents=amount_cents,
        currency=currency,
        evidence_url=evidence_url,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race against a concurrent writer for the same idempotency
        # tuple — roll back and return the winner's row as a replay.
        db.rollback()
        winner = db.execute(
            select(FunnelEvent).where(
                FunnelEvent.source_system == source_system,
                FunnelEvent.source_event_id == source_event_id,
                FunnelEvent.stage == stage,
            )
        ).scalar_one()
        return winner, True

    return row, False


def record_run(
    db: Session,
    *,
    job_id: str,
    loop_name: str,
    host: str,
    outcome: RunOutcome,
    rows_emitted: int = 0,
    note: str | None = None,
    ts: datetime | None = None,
) -> LoopRunLedger:
    """Record one loop_runs_ledger row — every job execution, not deduped.

    Unlike record_event, this is NOT idempotent by design: a loop that runs
    3 times in a day writes 3 rows, because loop_runs_ledger answers "did
    the flywheel run" (runs_last_24h in the summary), which is legitimately
    a count of executions, not of unique subjects.
    """
    row = LoopRunLedger(
        ts=ts or datetime.now(timezone.utc),
        id=uuid4(),
        job_id=job_id,
        loop_name=loop_name,
        host=host,
        outcome=outcome,
        rows_emitted=rows_emitted,
        note=note,
    )
    db.add(row)
    db.flush()
    return row
