"""GitHub repository_dispatch + issue-creation helper.

Default path: POST repository_dispatch to wisechef-ai/recipes-api (unchanged).
User-routable path: create a GitHub issue in the user's own repo via their
encrypted PAT (loopclose_3005 Phase J — THE MOAT).

Never raises — failure is logged and returns None so the API write is durable
even when GitHub is unavailable.

Security:
  - User tokens are passed in-memory only, never logged (only safe prefix).
  - ``verify_repo_access`` is the gate; dispatch is refused for repos the
    token does not cover.
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

    This is the **default path** — unchanged from pre-Phase-J.
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


def dispatch_issue(
    repo: str,
    token: str,
    *,
    title: str,
    body: str,
    labels: list[str] | None = None,
) -> str | None:
    """Create a GitHub issue in ``repo`` using ``token``.

    Phase J — user-routable feedback path.  Called only after
    ``verify_repo_access`` has confirmed the token covers the repo.

    Returns the issue URL on success, None on failure. Never raises.
    The token is NEVER logged.
    """
    try:
        from app.feedback_github import create_issue

        return create_issue(repo, token, title=title, body=body, labels=labels)
    # Rationale: catch all errors so user feedback dispatch never crashes the API write
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "dispatch_issue failed: repo=%s error=%s",
            repo,
            exc,
        )
        return None
