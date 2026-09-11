"""t_a2ba8443 — "Test connection" endpoint for the /library connect-agent card.

The funnel re-run t_a609be88 (CHEF-2026-09-10-E) showed the activation
bottleneck MOVED to revealed → first-use (0/2 users ever made one
authenticated call). The connect-agent card hands over the key + config
blocks and the user is on their own — there is zero post-reveal signal: we
cannot distinguish "never tried", "tried and failed", or "tried, worked,
but didn't install".

This endpoint is the card's "Test connection" button target. It is a
DELIBERATE x-api-key-authenticated surface (NOT under /api/auth/*, whose
JWT_AUTH_PREFIXES entry would bypass APIKeyMiddleware) so that a successful
test:

  1. flows through the SAME APIKeyMiddleware.dispatch x-api-key branch the
     user's real agent calls — including the last_used_at stamp
     (app.last_used_tracker), i.e. a successful test IS the first-use event
     the funnel measures, and
  2. records the new ``connect_agent.tested`` telemetry event SERVER-SIDE
     at the same place last_used_at is stamped — per the task decree and
     the bhint-tel0824 rule: funnel events are written SERVER-SIDE ONLY
     (direct row insert), never through POST /api/telemetry, so the
     endpoint's CLOSED event_type enum is NOT widened. The event only
     records SUCCESSFUL tests: the middleware 401s an invalid key before
     this handler ever runs, so a failure path is structurally "no row".

Auth posture: middleware 401s anything without a valid rec_/lsk_/rec_agent_
key. Agent keys additionally pass the agentreg_0819 revocation gate in the
same middleware branch. This handler reads request.state.auth_ctx (always
stamped for a non-exempt path that got this far).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.connect_agent_telemetry import record_connect_agent_event

router = APIRouter(tags=["connect-test"])


@router.get("/api/connect/test")
async def connect_test(request: Request, db: Session = Depends(get_db)):
    """Verify the caller's API key works and record connect_agent.tested.

    Reached only with a valid x-api-key (APIKeyMiddleware 401s otherwise,
    which is exactly the failure signal the portal surfaces as an
    actionable error). On success, 200 with the caller's key facts — the
    portal renders "✓ Connected" — and one telemetry row.
    """
    auth_ctx = getattr(request.state, "auth_ctx", None)
    if auth_ctx is None or auth_ctx.user_id is None:
        # Defensive only: every route to here has a stamped user ctx
        # (middleware 401s anonymous callers first). Never leak a success
        # shape without an authenticated principal.
        raise HTTPException(status_code=401, detail="Invalid API key")

    from app.config import settings
    from app.utils.client_ip import _real_client_ip

    try:
        _client_ip = _real_client_ip(request, settings.TRUSTED_PROXY_CIDRS)
    # Rationale: client_ip is observability-only; never fail the test on it.
    except Exception:  # noqa: BLE001
        _client_ip = None

    # t_a2ba8443: the decided event name. Server-side direct insert — the
    # /api/telemetry enum stays closed (pinned by the catel_0826 test).
    # payload.user_id rides along for the funnel join (TelemetryEvent has
    # no user column), same as connect_agent.shown / first_key.revealed.
    record_connect_agent_event(
        db,
        event_type="connect_agent.tested",
        user_id=str(auth_ctx.user_id),
        payload={
            "surface": "/library",
            "endpoint": "/api/connect/test",
            "api_key_id": str(auth_ctx.api_key_id) if auth_ctx.api_key_id else None,
        },
        client_ip=_client_ip,
        commit=True,  # route performs no other write — the row would be lost
    )

    return {
        "ok": True,
        "connected": True,
        "message": "Connection verified — your API key authenticated successfully.",
        "tier": auth_ctx.tier,
    }
