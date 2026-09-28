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
  paid             Stripe invoice id           source_system='stripe' (recurring)
                   Stripe payment_intent id     source_system='stripe-onetime' (Founding/one-time)

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
from datetime import datetime, timezone

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Bundle, FunnelEvent, InstallEvent, User
from app.services.funnel_ledger import classify, record_event, resolve_entity
from app.services.probe_detection import is_bench_slug

logger = logging.getLogger(__name__)

SOURCE_SYSTEM_APP = "loopskill-api"
SOURCE_SYSTEM_STRIPE = "stripe"
# flywheel_0902/B council v2 §0.9c: paid rows must carry a machine-checkable
# recurring-vs-one-time discriminator so the summary can split
# founding_cents from recurring_cents instead of reporting one blended
# total. Stripe mechanics make this discriminator free: mode=payment
# checkout sessions (the Founding SKU today, any future one-time SKU
# tomorrow) never produce an Invoice object — only a PaymentIntent — while
# every subscription payment DOES produce an Invoice. So "invoice-backed"
# IS "recurring" by construction; source_system carries that fact so the
# summary route never has to re-derive it (or drift from this backfill).
SOURCE_SYSTEM_STRIPE_ONETIME = "stripe-onetime"
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
            ts=user.created_at,
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


def _classify_install(event: InstallEvent, ip: str | None) -> tuple[str, str]:
    """Classify one install. Our own probes and bench runs are fleet (pricing0928 E2).

    ``is_probe`` is stamped at write time by app/services/probe_detection.py
    (fleet keys, probe IPs, and ``coldstart-bench-*`` slugs). The slug check is
    repeated here so installs recorded before that rule existed are caught too.
    """
    if getattr(event, "is_probe", False):
        return "fleet", "install_events.is_probe"
    if is_bench_slug(event.skill_slug):
        return "fleet", f"skill_slug:{event.skill_slug} is a benchmark throwaway"
    return classify(ip=ip)


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
        classification, evidence = _classify_install(event, ip)

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
            ts=event.created_at,
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
            ts=bundle.created_at,
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


def _invoice_backed_pi_ids(invoices: list[dict[str, Any]]) -> set[str]:
    """PaymentIntent ids an invoice says it was paid by.

    Covers both shapes: the legacy top-level ``invoice.payment_intent`` and
    the ``invoice.payments`` list (``payments.data[].payment.payment_intent``)
    that newer Stripe API versions return instead.
    """
    ids: set[str] = set()
    for inv in invoices:
        legacy = inv.get("payment_intent")
        if isinstance(legacy, dict):
            legacy = legacy.get("id")
        if legacy:
            ids.add(legacy)
        for pay in (inv.get("payments") or {}).get("data") or []:
            pi = (pay.get("payment") or {}).get("payment_intent")
            if isinstance(pi, dict):
                pi = pi.get("id")
            if pi:
                ids.add(pi)
    return ids


def _pi_is_invoice_backed(pi: dict[str, Any], invoice_ids: set[str], invoice_backed_pi_ids: set[str]) -> bool:
    """True when this PaymentIntent paid an invoice (so it is NOT one-time revenue).

    pricing0928 (t_7f5808d2, E5): Stripe API ``2026-08-26.dahlia`` dropped the
    top-level ``pi.invoice`` field that the original check relied on. The link
    now lives in ``pi.payment_details.order_reference`` (the ``in_...`` id) and
    on the invoice's ``payments`` list. With only the old check every
    subscription charge was counted twice: once as ``stripe`` and again as
    ``stripe-onetime`` (prod ledger: 12 + 12 rows for the same $12). Any one
    signal is enough.
    """
    if pi.get("invoice"):
        return True
    if pi.get("id") in invoice_backed_pi_ids:
        return True
    order_ref = (pi.get("payment_details") or {}).get("order_reference")
    return bool(order_ref) and (order_ref in invoice_ids or str(order_ref).startswith("in_"))


def _stripe_paid_source_ids(
    *, invoices: list[dict[str, Any]], payment_intents: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any], str]]:
    """Merge Stripe invoices + payment_intents into a deduped (id, obj, source_system) list.

    A PaymentIntent already linked to an invoice (``pi["invoice"]`` set) is
    dropped — its Invoice object is the canonical record for that charge.
    This is the paid-dedup invariant: the returned list's length equals the
    count of DISTINCT stripe ids that should become funnel_events rows.

    ``source_system`` distinguishes recurring (invoice-backed, always
    ``SOURCE_SYSTEM_STRIPE``) from one-time (non-invoice-backed PI, always
    ``SOURCE_SYSTEM_STRIPE_ONETIME``) — see the module-level constant
    docstrings for why "invoice-backed" IS "recurring" by Stripe
    construction. The summary route sums these two buckets separately
    (council v2 §0.9c: never one blended paid total).
    """
    merged: list[tuple[str, dict[str, Any], str]] = []
    seen_ids: set[str] = set()
    invoice_ids = {inv.get("id") for inv in invoices if inv.get("id")}
    invoice_backed_pi_ids = _invoice_backed_pi_ids(invoices)

    for invoice in invoices:
        if (invoice.get("amount_paid") or 0) <= 0:
            continue
        inv_id = invoice["id"]
        if inv_id in seen_ids:
            continue
        seen_ids.add(inv_id)
        merged.append((inv_id, invoice, SOURCE_SYSTEM_STRIPE))

    for pi in payment_intents:
        if pi.get("status") != "succeeded":
            continue
        if (pi.get("amount") or 0) <= 0:
            continue
        if _pi_is_invoice_backed(pi, invoice_ids, invoice_backed_pi_ids):
            # Invoice-backed — the Invoice object above already covers this
            # charge. Skipping here is the dedup the council's paid
            # invariant depends on.
            continue
        pi_id = pi["id"]
        if pi_id in seen_ids:
            continue
        seen_ids.add(pi_id)
        merged.append((pi_id, pi, SOURCE_SYSTEM_STRIPE_ONETIME))

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

    Each row's ``source_system`` is either ``SOURCE_SYSTEM_STRIPE``
    (recurring, invoice-backed) or ``SOURCE_SYSTEM_STRIPE_ONETIME``
    (Founding SKU / any future one-time SKU) — the summary route sums them
    into ``recurring_cents`` / ``founding_cents`` separately.
    """
    result = BackfillResult(stage="paid", dry_run=dry_run)
    merged = _stripe_paid_source_ids(invoices=invoices, payment_intents=payment_intents)

    for source_id, obj, source_system in merged:
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
                result.sample.append(
                    {
                        "source_event_id": source_id,
                        "source_system": source_system,
                        "classification": classification,
                    }
                )
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
            source_system=source_system,
            source_event_id=source_id,
            ts=datetime.fromtimestamp(int(obj.get("created") or 0), tz=timezone.utc)
            if obj.get("created")
            else None,
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


# ── pricing0928 (t_7f5808d2) corrections for rows already in the ledger ──────
#
# record_event never rewrites an existing row (a replay returns it unchanged),
# so fixing classify() and the paid dedup only helps rows written from now on.
# These two functions repair the rows written before the fix. Both are
# dry-run by default, idempotent, and scoped to the exact defect.


def prune_invoice_backed_onetime(
    db: Session,
    *,
    invoices: list[dict[str, Any]],
    payment_intents: list[dict[str, Any]],
    dry_run: bool = True,
) -> BackfillResult:
    """Delete ``stripe-onetime`` paid rows whose PaymentIntent paid an invoice.

    These rows are the E5 double count: the same charge already has its
    ``stripe`` (invoice) row. They are derived data, re-creatable from Stripe
    by re-running the backfill, so removing them loses nothing.
    """
    result = BackfillResult(stage="paid:prune-onetime-dupes", dry_run=dry_run)
    invoice_ids = {inv.get("id") for inv in invoices if inv.get("id")}
    backed = _invoice_backed_pi_ids(invoices)
    dup_ids = {
        pi["id"] for pi in payment_intents if pi.get("id") and _pi_is_invoice_backed(pi, invoice_ids, backed)
    }
    if not dup_ids:
        return result
    rows = (
        db.execute(
            select(FunnelEvent).where(
                FunnelEvent.stage == "paid",
                FunnelEvent.source_system == SOURCE_SYSTEM_STRIPE_ONETIME,
                FunnelEvent.source_event_id.in_(sorted(dup_ids)),
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        result.scanned += 1
        if len(result.sample) < 5:
            result.sample.append({"source_event_id": row.source_event_id, "amount_cents": row.amount_cents})
        if not dry_run:
            db.delete(row)
        result.written += 1
    if not dry_run:
        db.commit()
    return result


def reclassify_hosting_installs(db: Session, *, dry_run: bool = True) -> BackfillResult:
    """Re-classify ``installed:stranger`` rows with today's install rules.

    Hosting-network IPs become ``unknown`` (E1); probe and ``coldstart-bench-*``
    installs become ``fleet`` (E2). Only rows whose answer changed are rewritten.
    The old evidence is kept in the new evidence string for audit.
    """
    result = BackfillResult(stage="installed:reclassify-hosting", dry_run=dry_run)
    events = (
        db.execute(
            select(FunnelEvent).where(
                FunnelEvent.stage == "installed",
                FunnelEvent.source_system == SOURCE_SYSTEM_APP,
                FunnelEvent.classification == "stranger",
            )
        )
        .scalars()
        .all()
    )
    # Join in Python: source_event_id is str(install_events.id), and a SQL
    # UUID->text cast renders differently on SQLite (hex) and Postgres (dashed).
    install_ids = []
    for ev in events:
        try:
            install_ids.append(UUID(ev.source_event_id))
        except (ValueError, TypeError):
            continue
    events_by_id = {
        str(e.id): e
        for e in db.execute(select(InstallEvent).where(InstallEvent.id.in_(install_ids))).scalars().all()
    }
    rows = [(ev, getattr(events_by_id.get(ev.source_event_id), "client_ip", None)) for ev in events]
    for row, client_ip in rows:
        result.scanned += 1
        event = events_by_id.get(row.source_event_id)
        if event is None:
            result.skipped += 1
            continue
        classification, evidence = _classify_install(event, (client_ip or "").strip() or None)
        if classification == "stranger":
            result.skipped += 1
            continue
        if len(result.sample) < 5:
            result.sample.append(
                {"source_event_id": row.source_event_id, "to": classification, "evidence": evidence}
            )
        if not dry_run:
            row.classification = classification
            row.classification_evidence = (
                f"{evidence} [reclassified pricing0928; was: {row.classification_evidence}]"
            )
        result.written += 1
    if not dry_run:
        db.commit()
    return result
