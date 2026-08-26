"""Flywheel F1.2 — one-time reveal of the auto-minted first API key.

Split out of ``app/auth_routes.py`` to respect the W0.2 600-line module
gate (hard, never waived). The route logically pairs with the OAuth
callbacks (which stamp the reveal cookie via
``app.services.first_key_reveal.store_reveal``) but has no other coupling
to the auth module beyond the shared ``get_current_user_optional``
dependency.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app._config_block_formatter import build_config_blocks
from app.database import get_db
from app.services.first_key_reveal import REVEAL_COOKIE_NAME

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

_MCP_ENDPOINT = "https://app.loopskill.io/api/mcp/http"


@router.get("/first-key-reveal")
async def first_key_reveal(
    request: Request,
    db: Session = Depends(get_db),
):
    """One-time reveal of the plaintext of the auto-minted first API key.

    Reads the ephemeral reveal token from the ``wr_first_key_reveal`` cookie
    stamped by the OAuth callback (never from a query param — the plaintext
    itself never rides a URL). Requires the SAME authed session (wr_jwt) as
    the account that owns the token — this is not a public link. The stored
    entry is deleted on first successful read (see
    ``app.services.first_key_reveal.consume_reveal``), so refreshing the
    post-signup page a second time correctly returns 404 rather than
    re-showing the secret.

    Returns 404 (not 401) when there is nothing to reveal — this is the
    common case (returning user, or a first-time user whose reveal already
    expired/was consumed), not an error condition the portal needs to
    surface loudly.
    """
    from app.auth_routes import get_current_user_optional
    from app.services.connect_agent_telemetry import (
        EVENT_REVEALED,
        EVENT_SHOWN,
        record_connect_agent_event,
    )

    user = get_current_user_optional(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")

    # catel_0826 (t_76db433e): this fetch is the /library connect-agent card's
    # exclusive API call, and the card's gate order (dismissed? → has_api_keys?
    # → connection_verified? → this fetch → un-hide) means an authed hit here
    # is exactly "the card is rendering for an eligible member". Counted
    # server-side per the bhint-tel0824 funnel-event rule (never through
    # POST /api/telemetry — its event_type enum stays closed). Fail-quiet:
    # never breaks the reveal response.
    try:
        from app.config import settings
        from app.utils.client_ip import _real_client_ip

        _shown_ip = _real_client_ip(request, settings.TRUSTED_PROXY_CIDRS)
    # Rationale: client_ip is observability-only; never fail the reveal on it.
    except Exception:  # noqa: BLE001
        _shown_ip = None
    record_connect_agent_event(
        db,
        event_type=EVENT_SHOWN,
        user_id=str(user.id),
        payload={"surface": "/library", "endpoint": "/api/auth/first-key-reveal"},
        client_ip=_shown_ip,
        commit=True,  # this route returns/raises without another commit — the row would be lost
    )

    reveal_token = request.cookies.get(REVEAL_COOKIE_NAME)
    if not reveal_token:
        raise HTTPException(status_code=404, detail="nothing_to_reveal")

    from app.services.first_key_reveal import consume_reveal

    payload = consume_reveal(reveal_token, user.id)

    # Always clear the cookie on this response — one-shot regardless of hit/miss.
    if payload is None:
        resp = JSONResponse(status_code=404, content={"detail": "nothing_to_reveal"})
        resp.delete_cookie(REVEAL_COOKIE_NAME, path="/")
        return resp

    # Reuse the SAME config-block generator the cbt_ share-token flow uses
    # (app._config_block_formatter.build_config_blocks) so the paste-ready
    # Hermes YAML / Claude Desktop JSON presentation is identical, not a
    # second hand-rolled formatter drifting out of sync with the first.
    config_blocks = build_config_blocks(token=payload["key"], cookbook_id=None, server_url=_MCP_ENDPOINT)

    # catel_0826 (t_76db433e): one-time reveal succeeded — the second
    # micro-funnel event. Successful branch ONLY (payload non-None); the
    # generic-copy / no-key branch never records it. Fail-quiet.
    record_connect_agent_event(
        db,
        event_type=EVENT_REVEALED,
        user_id=str(user.id),
        payload={"surface": "/library", "prefix": payload["prefix"], "label": payload["label"]},
        client_ip=_shown_ip,
        commit=True,
    )

    resp = JSONResponse(
        content={
            "key": payload["key"],
            "prefix": payload["prefix"],
            "label": payload["label"],
            "mcp_endpoint": _MCP_ENDPOINT,
            "config_blocks": config_blocks,
            "warning": "Save this key now — it will not be shown again.",
        }
    )
    resp.delete_cookie(REVEAL_COOKIE_NAME, path="/")
    return resp
