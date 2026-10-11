"""perclient_1110 — ``POST /api/pv``: first-party anonymous page-view beacon.

Why: app.loopskill.io has no access log, so there is no other way to count
visits to a portal page. The static portal calls ``navigator.sendBeacon``
(Content-Type text/plain) or same-origin ``fetch`` (application/json); either
way the raw body is parsed as JSON here, so no content-type is required.

Contract: the response is ALWAYS an empty 204 — valid, junk, oversized,
non-allowlisted, rate-limited or a failed write all look identical to the
caller. Anonymous by design: ``/api/pv`` is in APIKeyMiddleware's
PUBLIC_POST_ONLY_PATHS. Parsing/classification lives in app/services/pageview.py.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import TelemetryEvent
from app.services import pageview
from app.utils.client_ip import _real_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(tags=["telemetry"])


async def _bounded_body(request: Request) -> bytes | None:
    """Read at most MAX_BODY_BYTES (+1); ``None`` when the body is too big."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > pageview.MAX_BODY_BYTES:
        return None
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > pageview.MAX_BODY_BYTES:
            return None
    return bytes(buf)


@router.post("/api/pv", status_code=204, response_class=Response)
def record_page_view(
    request: Request,
    raw: bytes | None = Depends(_bounded_body),
    db: Session = Depends(get_db),
) -> Response:
    """Record one page view; always 204 with no body."""
    beacon = pageview.parse_beacon(raw)
    if beacon is None:
        return Response(status_code=204)
    try:
        client_ip = _real_client_ip(request, settings.TRUSTED_PROXY_CIDRS)
    # Rationale: same posture as the install/telemetry writers — an IP parse
    # failure must never turn a beacon into a 5xx.
    except Exception:  # noqa: BLE001
        client_ip = None
    if client_ip in ("", "unknown"):
        client_ip = None
    if not pageview.allow(client_ip or "unknown"):
        return Response(status_code=204)
    try:
        payload = pageview.build_payload(
            beacon,
            client_ip=client_ip,
            user_agent=request.headers.get("user-agent"),
        )
        db.add(
            TelemetryEvent(
                event_type="page_view",
                skill_slug=None,
                client_ip=client_ip[:64] if client_ip else None,
                payload=payload,
            )
        )
        db.commit()
    # Rationale: a beacon is best-effort analytics; a DB hiccup must not
    # surface as a 5xx (never an oracle). Roll back and log instead.
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.warning("page_view beacon write failed", exc_info=True)
    return Response(status_code=204)
