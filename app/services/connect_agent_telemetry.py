"""Activation telemetry for the /library "Connect your agent" card (flywheel F1.2).

From funnel analysis t_1c11801c (CHEF-2026-08-25-E): the signup→first-install
funnel was unmeasurable between signup and install. The F1.2 connect-agent
card on /library (portal PR #80) is the critical hand-off surface but emitted
zero telemetry — when the next external cohort arrives there is no way to tell
whether signups saw the card, revealed their key, or copied the config.

Event vocabulary (event_type, an open string column — no schema change):

    connect_agent.shown      the card rendered for an authed, key-holding,
                             not-yet-connected member (any card variant)
    first_key.revealed       the one-time plaintext reveal succeeded

FIRE-POINT DEVIATION FROM TASK SPEC (documented on the kanban card): the task
decreed client-side POSTs to /api/telemetry from ``maybeShowConnectAgentCard``
in library.astro. Verified against the code before implementing: that endpoint
enforces a CLOSED event_type enum (TELEMETRY_EVENT_TYPES, app/schemas.py) and
app/services/bundle_hint_telemetry.py — the exact pattern this module mirrors —
records the repo rule that funnel events are "written SERVER-SIDE ONLY (direct
row insert), never through that endpoint, so the enum guard does not apply and
must not be widened". The two events are therefore written server-side, at the
API calls the card itself makes, preserving the decided names and semantics:

    connect_agent.shown   fired on GET /api/auth/first-key-reveal after the
                          session resolves (the card's exclusive fetch —
                          gate order in maybeShowConnectAgentCard is
                          dismissed? → has_api_keys? → connection_verified?
                          → fetch first-key-reveal → un-hide). A hit here
                          means every gate passed and the card is about to
                          render; the only over-count is a JS error between
                          the fetch resolving and ``card.hidden = false``.
    first_key.revealed    fired in the successful reveal branch only (payload
                          non-None from consume_reveal) — NOT on the generic
                          no-key placeholder branch, matching the spec.

Why TelemetryEvent and not a new table: the funnel query needs per-user
attribution (payload.user_id) + counts + timestamps, and the legacy JSON
payload column is the established carrier for namespaced funnel events
(metasearch.install-intent, bundle_hint.*) — same decision recorded in
bundle_hint_telemetry.py for the identical requirement.

Fail-quiet contract: ``record_connect_agent_event`` NEVER raises. Telemetry is
observability; a telemetry hiccup must not break the reveal endpoint's
contract (200-with-key on hit, 404 on miss) — mirrors the bhint-tel0824 and
metasearch funnel-event rationale.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import TelemetryEvent

logger = logging.getLogger(__name__)

#: The connect-agent card rendered for an eligible member (any variant).
EVENT_SHOWN = "connect_agent.shown"
#: The one-time first-key plaintext reveal succeeded.
EVENT_REVEALED = "first_key.revealed"


def record_connect_agent_event(
    db: Session,
    *,
    event_type: str,
    user_id: str | None,
    payload: dict[str, Any],
    client_ip: str | None = None,
    commit: bool = False,
) -> None:
    """Write one activation telemetry row. Fire-and-forget: never raises.

    Args:
        event_type: one of EVENT_SHOWN / EVENT_REVEALED.
        user_id: the authed member the card is rendering for (str UUID) —
            TelemetryEvent has no user column, so it ALSO rides in the JSON
            payload for the funnel join.
        payload: small JSON dict (surface / variant facts).
        client_ip: the requester's real IP, when cheap to obtain.
        commit: commit immediately (single-event call sites); False leaves
            the row in the caller's transaction.
    """
    enriched = dict(payload)
    if user_id is not None:
        enriched.setdefault("user_id", user_id)
    # Rationale: observability only — a telemetry write failure must never
    # surface on the reveal endpoint's own response contract (same contract
    # as bundle_hint_telemetry / metasearch funnel events).
    try:
        db.add(
            TelemetryEvent(
                event_type=event_type,
                skill_slug=None,
                payload=json.dumps(enriched, default=str),
                client_ip=client_ip,
            )
        )
        if commit:
            db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("connect_agent telemetry write failed (%s)", event_type, exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
