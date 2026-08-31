"""Telemetry + observability for the bundle fast-path hint (t_55a1a333).

bhint0823 (t_8ccbdbc5) shipped the hint itself; Tori's 2026-08-23 ship review
flagged that nothing anywhere recorded the hint FIRING, so conversion could
never be measured and a hinting regression would die silently. This module
is the follow-up: every hint outcome lands as ONE ``TelemetryEvent`` row on
the existing telemetry rail — no new table, no new hot-path migration, no
counter table writes (spec: "piggyback on existing telemetry/counter rails").

Event vocabulary (event_type, an open string column — no schema change):

    bundle_hint.shown        hint computed non-null and stamped on a response
    bundle_hint.error        compute_bundle_hint raised (fail-quiet kept: the
                             install response is still 200, hint absent)
    bundle_hint.converted_pull
                             GET /api/bundles/install.sh (the exact command
                             the hint teaches) fetched from the SAME client_ip
                             that was shown a hint for that slug, within 7d
                             of the shown event. Written by
                             bundle_install_script_routes at serve time.

Why TelemetryEvent and not a counter table: the retro query needs per-slug
counts + the (client_ip, slug, ts) join for conversion — a bare counter
cannot answer "did the hinted user pull", and install_events is append-only
install truth that must not grow hint-side-effect rows. TelemetryEvent
already carries client_ip + skill_slug + a JSON payload column, and
metasearch_routes.py sets the exact precedent for namespaced funnel events
("metasearch.install-intent") written fire-and-forget from a route.

skill_slug NOTE: POST /api/telemetry validates event_type against a closed
enum — these events are written SERVER-SIDE ONLY (direct row insert, same
as the metasearch funnel events), never through that endpoint, so the enum
guard does not apply and must not be widened.

Fail-quiet contract: ``record_bundle_hint_event`` NEVER raises. Telemetry is
observability; a telemetry hiccup must not 500 an install that already
committed (mirrors the metasearch funnel-event rationale and the hint's own
fail-quiet design).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import TelemetryEvent

logger = logging.getLogger(__name__)

#: Hint shown to the client in a response body/header.
EVENT_SHOWN = "bundle_hint.shown"
#: compute_bundle_hint raised; response stayed 200 with no hint.
EVENT_ERROR = "bundle_hint.error"
#: The hinted install.sh was pulled from the same client_ip within 7d.
EVENT_CONVERTED_PULL = "bundle_hint.converted_pull"

#: Conversion attribution window (task spec: same client_ip within 7d).
CONVERSION_WINDOW_DAYS = 7


def record_bundle_hint_event(
    db: Session,
    *,
    event_type: str,
    client_ip: str | None,
    payload: dict[str, Any],
    skill_slug: str | None = None,
    commit: bool = False,
) -> None:
    """Write one hint telemetry row. Fire-and-forget: never raises.

    Args:
        event_type: one of EVENT_SHOWN / EVENT_ERROR / EVENT_CONVERTED_PULL.
        client_ip: the InstallEvent.client_ip the hint was computed for (or
            the puller's real IP on the conversion path).
        payload: small JSON dict (matched / install_all / error text).
        skill_slug: the hinted bundle slug, stored in the indexed
            skill_slug column so the retro query groups by slug directly.
        commit: commit immediately (single-event call sites); False leaves
            the row in the caller's transaction.
    """
    # Rationale: observability only — a telemetry write failure must never
    # surface on an install response that already committed (same contract
    # as the hint itself and the metasearch funnel events).
    try:
        db.add(
            TelemetryEvent(
                event_type=event_type,
                skill_slug=skill_slug,
                payload=json.dumps(payload, default=str),
                client_ip=client_ip,
            )
        )
        if commit:
            db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("bundle_hint telemetry write failed (%s)", event_type, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def observe_hint_after_install(
    db: Session,
    *,
    validated_bundle_id,
    event,
) -> dict | None:
    """Compute + observe the bundle hint for a committed direct install.

    bhint0823 had this inline in app/install_routes.py; bhint-tel0824
    (t_55a1a333) added the shown/error telemetry and the inline block grew
    past the W0.2 god-object gate (>600 lines per module). Extracted here so
    the route stays a thin caller: compute (fail-quiet), count every fire
    (bundle_hint.shown), and count + log every failure (bundle_hint.error +
    one structured ERROR line). Returns the hint payload or None; NEVER
    raises — the install response must stay 200 regardless.
    """
    from app.services.bundle_hint import compute_bundle_hint

    client_ip = getattr(event, "client_ip", None)

    if validated_bundle_id is not None:
        # Bundle-attributed installs are already on the fast path — no hint.
        return None

    # Rationale: observability/onboarding hint only; an internal failure
    # (DB hiccup mid-hint) must not fail an install that already committed.
    # bhint-tel0824: on that failure, count it (bundle_hint.error telemetry
    # row + one structured log line) instead of swallowing it invisibly.
    try:
        hint = compute_bundle_hint(db, client_ip=client_ip)
    except Exception as exc:  # noqa: BLE001
        logger.exception("bundle_hint computation failed", extra={"client_ip": client_ip, "error": repr(exc)})
        record_bundle_hint_event(
            db,
            event_type="bundle_hint.error",
            client_ip=client_ip,
            payload={"error": repr(exc)[:512]},
            commit=True,
        )
        return None

    if hint is None:
        return None

    # bhint-tel0824 (t_55a1a333): count every hint fire, slug-keyed, on the
    # telemetry rail — without this the hint's conversion could never be
    # measured (Tori ship-review gap 1). One row per fire; the telemetry
    # helper never raises, so the already-committed install response can't
    # be failed by its own observability.
    record_bundle_hint_event(
        db,
        event_type="bundle_hint.shown",
        client_ip=client_ip,
        skill_slug=hint["slug"],
        payload={"slug": hint["slug"], "matched": hint["matched"]},
        commit=True,
    )
    return hint


def maybe_record_hint_conversion(
    db: Session,
    *,
    client_ip: str | None,
    bundle_slug: str | None,
    now=None,
) -> bool:
    """Record a converted_pull iff this IP was shown this slug within 7d.

    Called by the /api/bundles/install.sh serve path (the exact command the
    hint teaches). Server-side attribution only: an anonymous visitor who
    was never hinted gets NO row — organic install.sh pulls stay uncounted,
    so the converted number can never be inflated by unrelated traffic.

    Returns True when a conversion row was written.
    """
    if not client_ip or not bundle_slug:
        return False

    from datetime import UTC, datetime, timedelta

    from app.models import TelemetryEvent as TE

    now = now or datetime.now(UTC)
    window_start = now - timedelta(days=CONVERSION_WINDOW_DAYS)

    hinted = (
        db.query(TE.id)
        .filter(
            TE.event_type == EVENT_SHOWN,
            TE.client_ip == client_ip,
            TE.skill_slug == bundle_slug,
            TE.created_at >= window_start,
        )
        .first()
    )
    if hinted is None:
        return False

    record_bundle_hint_event(
        db,
        event_type=EVENT_CONVERTED_PULL,
        client_ip=client_ip,
        skill_slug=bundle_slug,
        payload={"slug": bundle_slug, "attributed_to": EVENT_SHOWN},
        commit=True,
    )
    return True
