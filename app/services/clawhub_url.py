"""ClawHub URL construction — owner-scoped deep links.

ClawHub skill pages are **owner-scoped**::

    https://clawhub.ai/<ownerHandle>/skills/<slug>     # 200, renders

The bare form we used to mint is NOT a valid page. ClawHub 307-redirects it to
a doubled path that renders a client-side "We couldn't find that page."::

    https://clawhub.ai/skills/<slug>
      -> 307 Location: /skills/skills/<slug>
      -> 200  (SPA shell; the router renders not-found)

The dead URL answers **HTTP 200** with a correct-looking ``<title>`` — it is a
soft-404. Status-code canaries cannot detect it; only DOM rendering can. See
issue #139 for the measured blast radius (69,150 rows, 76% of the index).

Because ClawHub is ``install_path=deep_link`` by policy (superset_0606 decision
#6 — index and link, never rehost), the deep link is the *entire* deliverable
for these rows: a broken ``origin_url`` makes the row worthless, not degraded.

Where the owner handle comes from:

* ``GET /api/v1/skills/{slug}`` -> ``owner.handle`` (callers that already fetch
  the detail payload get it for free — pass it in)
* ``GET /api/search?q={slug}``  -> ``ownerHandle`` (resolver fallback here)

It is NOT in the Hub snapshot row and NOT in the v1 list API, so ingest of
69k rows cannot resolve owners inline — those rows fail safe to the browse
page (see ``CLAWHUB_BROWSE_URL``).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

CLAWHUB_BASE = "https://clawhub.ai"

#: Fail-safe target when the owner handle is unknown. The browse page is
#: verified to render (2026-07-26). Do NOT fall back to a *guessed* detail URL,
#: and do NOT use ``/skills?q=<slug>`` — that route is itself broken upstream
#: (crashes with "Cannot read properties of undefined (reading 'owner')").
#: A working browse link beats a confident 404.
CLAWHUB_BROWSE_URL = f"{CLAWHUB_BASE}/skills"

#: ClawHub handles and slugs are plain URL tokens. Anything else (path
#: traversal, query/fragment injection, whitespace) is rejected rather than
#: interpolated — same posture as ``hub_repo_path.is_safe_repo_ident``.
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")


def is_safe_token(value: str | None) -> bool:
    """True if ``value`` is a plain URL token safe to interpolate into a path."""
    if not value:
        return False
    token = value.strip()
    return bool(token) and len(token) <= 128 and bool(_SAFE_TOKEN.match(token))


def clawhub_skill_url(slug: str | None, owner: str | None = None) -> str:
    """Build the canonical ClawHub URL for ``slug``.

    Returns the owner-scoped deep link when ``owner`` is a safe token, and the
    browse page otherwise. Never returns the bare ``/skills/<slug>`` form — it
    is a soft-404 (see module docstring).
    """
    if not is_safe_token(slug):
        return CLAWHUB_BROWSE_URL
    if not is_safe_token(owner):
        return CLAWHUB_BROWSE_URL
    return f"{CLAWHUB_BASE}/{owner.strip()}/skills/{slug.strip()}"  # type: ignore[union-attr]


def owner_from_detail_payload(data: Any) -> str | None:
    """Extract ``owner.handle`` from a ``/api/v1/skills/{slug}`` response.

    Callers that already fetched the detail payload should use this instead of
    paying for a second network round trip.
    """
    if not isinstance(data, dict):
        return None
    owner = data.get("owner")
    if not isinstance(owner, dict):
        return None
    handle = owner.get("handle")
    return handle.strip() if isinstance(handle, str) and is_safe_token(handle) else None


#: Process-local memo for resolved owners. ClawHub owner handles are stable
#: (they are the account identity), so a per-process cache is safe and keeps a
#: page of search results to at most one lookup per distinct slug.
#: Bounded so a hostile/large query set cannot grow it without limit.
_OWNER_CACHE: dict[str, str | None] = {}
_OWNER_CACHE_MAX = 4096


def resolve_owner(slug: str | None) -> str | None:
    """Look up a ClawHub skill's owner handle via ``GET /api/search``.

    Fail-safe: returns ``None`` on any transport/parse error or unsafe value,
    which makes :func:`clawhub_skill_url` degrade to the browse page rather
    than mint a dead deep link. Negative results are cached too — a slug that
    upstream cannot resolve should not be retried on every request.
    """
    if not is_safe_token(slug):
        return None
    key = slug.strip()  # type: ignore[union-attr]
    if key in _OWNER_CACHE:
        return _OWNER_CACHE[key]

    owner: str | None = None
    try:
        from app.services import federation_live as fl

        data = fl._safe_json_get(f"{CLAWHUB_BASE}/api/search", params={"q": key})
        results = data.get("results") if isinstance(data, dict) else None
        for row in results or []:
            if isinstance(row, dict) and row.get("slug") == key:
                handle = row.get("ownerHandle")
                if isinstance(handle, str) and is_safe_token(handle):
                    owner = handle.strip()
                break
    # Rationale: owner lookup is a best-effort enrichment — a transport or
    # parse failure must degrade to the browse-page fallback, never 500 a route.
    except Exception:  # noqa: BLE001
        logger.warning("clawhub owner lookup failed for %s", key, exc_info=True)
        return None

    if len(_OWNER_CACHE) < _OWNER_CACHE_MAX:
        _OWNER_CACHE[key] = owner
    return owner
