"""flywheel_0902/B — backfill: turn existing prod rows into funnel_events.

Idempotent (safe to re-run any number of times — record_event dedupes on
the immutable source tuple). Dry-run by DEFAULT so a first invocation never
writes without an explicit ``--live`` flag.

Backfills four stages, each keyed to the row's OWN primary key as
``source_event_id`` (so re-running never double-counts and matches the
council's "idem_key = the immutable source tuple" correction exactly):

  signup          users.id                    source_system='loopskill-api'
  installed        install_events.id           source_system='loopskill-api'
  bundle_created   bundles.id                  source_system='loopskill-api'
  paid             Stripe invoice / payment_intent id   source_system='stripe'

Classification:
  signup         — email vs config/fleet_exclusions.yaml
  installed      — client_ip vs the same list; NULL client_ip => unknown,
                   NEVER stranger (council v2 §0.9 — the exact false-green
                   bug this backfill must not reintroduce)
  bundle_created — resolved owner's email, same rule as signup
  paid           — resolved customer's linked User.email, same rule

Paid dedup (council invariant): a subscription's first payment_intent is
often ALSO reflected by an Invoice object. This backfill sources paid rows
from two Stripe endpoints — Invoice.list(status="paid") and
PaymentIntent.list(status="succeeded") — and DROPS any PaymentIntent that
is already linked to an invoice (``invoice`` field set) so the same charge
is never counted twice. The invariant this buys: ledger paid-row count for
a run == the count of DISTINCT stripe ids fed into it (invoice ids MINUS
invoice-linked payment_intent ids that were skipped).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Bundle, InstallEvent, User
from app.services.funnel_ledger import classify, record_event, resolve_entity

logger = logging.getLogger(__name__)

SOURCE_SYSTEM_APP = "loopskill-api"
SOURCE_SYSTEM_STRIPE = "stripe"
BACKFILL_LOOP_NAME = "funnel-backfill"


@dataclass
class BackfillResult:
    stage: str
    scanned: int = 0
    written: int = 0
    replayed: int = 0
    skipped: int = 0
    dry_run: bool = True
    sample: list[dict[str, Any]] = field(default_factory=list)


def _entity_for_email(db: Session, email: str | None) -> tuple[str | None, str, str]:
    """Resolve an entity for an email (or none — anonymous), and classify.

    Returns (entity_kind_used, entity_id_str, classification).
    """
    if not email:
        return None, "", "unknown"
    classification, _evidence = classify(email=email)
    entity_id = resolve_entity(db, "email", email)
    return "email", str(entity_id), classification


def backfill_signup(db: Session, *, host: str, dry_run: bool = True) -> BackfillResult:
    """users.created_at → funnel_events(stage='signup')."""
    result = BackfillResult(stage="signup", dry_run=dry_run)
    users = db.execute(select(User)).scalars().all()

    for user in users:
        result.scanned += 1
        email = (user.email or "").strip().lower() or None
        classification, evidence = classify(email=email)

        if dry_run:
            result.written += 1
            if len(result.sample) < 5:
                result.sample.append({"source_event_id": str(user.id), "classification": classification})
            continue

        entity_id = (
            resolve_entity(db, "email", email) if email else resolve_entity(db, "user_id", str(user.id))
        )
        _row, replay = record_event(
            db,
            stage="signup",
            entity_id=entity_id,
            source_system=SOURCE_SYSTEM_APP,
            source_event_id=str(user.id),
            source_loop=BACKFILL_LOOP_NAME,
            host=host,
            classification=classification,
            classification_evidence=evidence,
        )
        if replay:
            result.replayed += 1
        else:
            result.written += 1

    if not dry_run:
        db.commit()
    return result


def backfill_installed(db: Session, *, host: str, dry_run: bool = True) -> BackfillResult:
    """install_events → funnel_events(stage='installed').

    NULL client_ip classifies unknown, never stranger — this is the exact
    false-green case the council flagged in the original design.
    """
    result = BackfillResult(stage="installed", dry_run=dry_run)
    events = db.execute(select(InstallEvent)).scalars().all()

    for event in events:
        result.scanned += 1
        ip = (event.client_ip or "").strip() or None
        classification, evidence = classify(ip=ip)

        if dry_run:
            result.written += 1
            if len(result.sample) < 5:
                result.sample.append({"source_event_id": str(event.id), "classification": classification})
            continue

        entity_id = (
            resolve_entity(db, "ip", ip) if ip else resolve_entity(db, "user_id", f"install:{event.id}")
        )
        _row, replay = record_event(
            db,
            stage="installed",
            entity_id=entity_id,
            source_system=SOURCE_SYSTEM_APP,
            source_event_id=str(event.id),
            source_loop=BACKFILL_LOOP_NAME,
            host=host,
            classification=classification,
            classification_evidence=evidence,
        )
        if replay:
            result.replayed += 1
        else:
            result.written += 1

    if not dry_run:
        db.commit()
    return result


def backfill_bundle_created(db: Session, *, host: str, dry_run: bool = True) -> BackfillResult:
    """bundles.created_at → funnel_events(stage='bundle_created')."""
    result = BackfillResult(stage="bundle_created", dry_run=dry_run)
    bundles = db.execute(select(Bundle)).scalars().all()

    for bundle in bundles:
        result.scanned += 1
        owner = db.get(User, bundle.bundle_owner) if bundle.bundle_owner else None
        email = (owner.email or "").strip().lower() if owner and owner.email else None
        classification, evidence = classify(email=email)

        if dry_run:
            result.written += 1
            if len(result.sample) < 5:
                result.sample.append({"source_event_id": str(bundle.id), "classification": classification})
            continue

        entity_id = (
            resolve_entity(db, "email", email)
            if email
            else resolve_entity(db, "user_id", f"bundle:{bundle.id}")
        )
        _row, replay = record_event(
            db,
            stage="bundle_created",
            entity_id=entity_id,
            source_system=SOURCE_SYSTEM_APP,
            source_event_id=str(bundle.id),
            source_loop=BACKFILL_LOOP_NAME,
            host=host,
            classification=classification,
            classification_evidence=evidence,
        )
        if replay:
            result.replayed += 1
        else:
            result.written += 1

    if not dry_run:
        db.commit()
    return result


def _stripe_paid_source_ids(
    *, invoices: list[dict[str, Any]], payment_intents: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """Merge Stripe invoices + payment_intents into a deduped (id, obj) list.

    A PaymentIntent already linked to an invoice (``pi["invoice"]`` set) is
    dropped — its Invoice object is the canonical record for that charge.
    This is the paid-dedup invariant: the returned list's length equals the
    count of DISTINCT stripe ids that should become funnel_events rows.
    """
    merged: list[tuple[str, dict[str, Any]]] = []
    seen_ids: set[str] = set()

    for invoice in invoices:
        if (invoice.get("amount_paid") or 0) <= 0:
            continue
        inv_id = invoice["id"]
        if inv_id in seen_ids:
            continue
        seen_ids.add(inv_id)
        merged.append((inv_id, invoice))

    for pi in payment_intents:
        if pi.get("status") != "succeeded":
            continue
        if (pi.get("amount") or 0) <= 0:
            continue
        if pi.get("invoice"):
            # Invoice-backed — the Invoice object above already covers this
            # charge. Skipping here is the dedup the council's paid
            # invariant depends on.
            continue
        pi_id = pi["id"]
        if pi_id in seen_ids:
            continue
        seen_ids.add(pi_id)
        merged.append((pi_id, pi))

    return merged


def backfill_paid(
    db: Session,
    *,
    host: str,
    invoices: list[dict[str, Any]],
    payment_intents: list[dict[str, Any]],
    dry_run: bool = True,
) -> BackfillResult:
    """Stripe paid invoices + non-invoice-backed succeeded PIs → funnel_events.

    ``invoices``/``payment_intents`` are pre-fetched lists (list of
    stripe-object-shaped dicts, i.e. already ``.to_dict()``'d per Stripe SDK
    15.x convention) so this function has no direct Stripe API dependency
    and is fully unit-testable. ``scripts/funnel_backfill.py`` is the only
    caller that actually calls the Stripe SDK.
    """
    result = BackfillResult(stage="paid", dry_run=dry_run)
    merged = _stripe_paid_source_ids(invoices=invoices, payment_intents=payment_intents)

    for source_id, obj in merged:
        result.scanned += 1
        customer_id = obj.get("customer")
        user = (
            db.execute(select(User).where(User.stripe_customer_id == customer_id)).scalar_one_or_none()
            if customer_id
            else None
        )
        email = (user.email or "").strip().lower() if user and user.email else None
        classification, evidence = classify(email=email)
        amount_cents = int(obj.get("amount_paid") or obj.get("amount") or 0)
        currency = obj.get("currency")

        if dry_run:
            result.written += 1
            if len(result.sample) < 5:
                result.sample.append({"source_event_id": source_id, "classification": classification})
            continue

        entity_id = (
            resolve_entity(db, "email", email)
            if email
            else resolve_entity(db, "stripe_customer", customer_id or f"unknown:{source_id}")
        )
        _row, replay = record_event(
            db,
            stage="paid",
            entity_id=entity_id,
            source_system=SOURCE_SYSTEM_STRIPE,
            source_event_id=source_id,
            source_loop=BACKFILL_LOOP_NAME,
            host=host,
            classification=classification,
            classification_evidence=evidence,
            amount_cents=amount_cents,
            currency=currency,
        )
        if replay:
            result.replayed += 1
        else:
            result.written += 1

    if not dry_run:
        db.commit()
    return result


def run_full_backfill(
    db: Session,
    *,
    host: str,
    invoices: list[dict[str, Any]] | None = None,
    payment_intents: list[dict[str, Any]] | None = None,
    dry_run: bool = True,
) -> list[BackfillResult]:
    """Run all four backfill stages in order. Stripe args optional (skip paid)."""
    results = [
        backfill_signup(db, host=host, dry_run=dry_run),
        backfill_installed(db, host=host, dry_run=dry_run),
        backfill_bundle_created(db, host=host, dry_run=dry_run),
    ]
    if invoices is not None or payment_intents is not None:
        results.append(
            backfill_paid(
                db,
                host=host,
                invoices=invoices or [],
                payment_intents=payment_intents or [],
                dry_run=dry_run,
            )
        )
    return results
