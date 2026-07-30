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

#: Dot-only tokens are RELATIVE PATH SEGMENTS, not names.
#:
#: The charset above intentionally allows ``.`` (real handles and slugs contain
#: it — ``llama.cpp``, ``next.js``). But a token made ENTIRELY of dots is a
#: traversal segment: ``clawhub_skill_url("aigate", "..")`` produced
#: ``https://clawhub.ai/../skills/aigate``, which every HTTP client normalises
#: to ``https://clawhub.ai/skills/aigate`` — the exact bare form that 307s to
#: the ``/skills/skills/<slug>`` soft-404 this module exists to prevent.
#: Verified live 2026-07-26: that URL returns 307 → /skills/skills/aigate.
#:
#: So the guard against minting the soft-404 could be bypassed by the very
#: value it was supposed to reject, and because a soft-404 answers HTTP 200
#: nothing downstream would ever have noticed. Caught by
#: ``test_spotify_2607_clawhub_owner_backfill.py`` (spotify_2607/0).
#:
#: ``a..b`` stays legal — it is a name containing dots, not a path segment.
_DOTS_ONLY = re.compile(r"^\.+$")


def is_safe_token(value: str | None) -> bool:
    """True if ``value`` is a plain URL token safe to interpolate into a path.

    Rejects anything that is not ``[A-Za-z0-9._-]+``, anything over 128 chars,
    and any dot-only token (``.``, ``..``, ``...``) — see ``_DOTS_ONLY``: those
    are relative path segments that silently rewrite the URL rather than name
    a resource.
    """
    if not value:
        return False
    token = value.strip()
    if not token or len(token) > 128:
        return False
    if _DOTS_ONLY.match(token):
        return False
    return bool(_SAFE_TOKEN.match(token))


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


def prime_owner_cache(resolved: dict[str, str]) -> int:
    """Seed :data:`_OWNER_CACHE` from a persisted ``identifier -> owner`` map.

    Issue #148. ``resolve_owner`` above issues ONE live
    ``GET clawhub.ai/api/search`` per uncached slug, and ``ClawHubAdapter._map``
    calls it once per row — so a 50-row search page fires up to 50 sequential
    upstream requests. Measured on live prod: cold ClawHub 59s (2026-07-26),
    re-measured 2026-07-30 at >90s (timeout), against 0.62s for all six other
    sources COMBINED. Warm it is 0.34s, so the upstream is healthy — the
    per-row fetch pattern is the defect.

    ``FederationHubSkill.owner_handle`` already persists this mapping (populated
    by the sp2607_0 backfill) and ``hub_owner_carry.load_resolved_owner_handles``
    already reads it in ONE query — but only the ingest path used it. This lets a
    request thread (which HAS a db session) pre-seed the process-local cache, so
    the adapter's per-row lookups become dict hits instead of HTTP calls.

    Deliberately does NOT overwrite existing entries: a live-resolved value (and
    a negative result, cached as ``None``) is at least as fresh as the snapshot.

    Returns the number of entries actually added, for logging/telemetry.
    """
    added = 0
    for identifier, owner in resolved.items():
        if len(_OWNER_CACHE) >= _OWNER_CACHE_MAX:
            break
        if not is_safe_token(identifier) or not is_safe_token(owner):
            # Re-validate at the boundary. load_resolved_owner_handles already
            # validates on the way out of the DB, but this function is public and
            # must not trust its caller to have done so — an unsafe handle
            # interpolated into a published URL is exactly the bug #139 fixed.
            continue
        key = identifier.strip()
        if key not in _OWNER_CACHE:
            _OWNER_CACHE[key] = owner.strip()
            added += 1
    return added
