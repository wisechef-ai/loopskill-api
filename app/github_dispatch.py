"""GitHub repository_dispatch helper.

Sends a repository_dispatch event to wisechef-ai/recipes-api.
Never raises — failure is logged and returns None so the API write is durable
even when GitHub is unavailable.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_REPO = "wisechef-ai/recipes-api"
_DISPATCH_URL = f"https://api.github.com/repos/{_REPO}/dispatches"


def dispatch_event(event_type: str, payload: dict[str, Any]) -> bool | None:
    """POST repository_dispatch to wisechef-ai/recipes-api.

    Returns True on success, None on failure. Never raises — failure logs and
    returns None so the API write is durable even if GitHub is down.
    """
    pat = os.environ.get("GITHUB_DISPATCH_PAT", "")
    if not pat:
        logger.warning(
            "GITHUB_DISPATCH_PAT is not set — skipping GitHub dispatch for event_type=%s",
            event_type,
        )
        return None

    try:
        import json as _json
        import urllib.request

        body = _json.dumps(
            {
                "event_type": event_type,
                "client_payload": payload,
            }
        ).encode()

        req = urllib.request.Request(
            _DISPATCH_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {pat}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )

        import urllib.error

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                logger.info("github dispatch sent: event_type=%s status=%s", event_type, status)
        except urllib.error.HTTPError as e:
            logger.warning(
                "github dispatch HTTP error: event_type=%s status=%s body=%s",
                event_type,
                e.code,
                e.read(256),
            )
            return None

        return True

    # Rationale: catch all network/JSON/OS errors so dispatch failure never propagates to caller
    except Exception as exc:  # noqa: BLE001
        logger.warning("github dispatch failed: event_type=%s error=%s", event_type, exc)
        return None
