"""Request-thread priming of the ClawHub owner cache (issue #148).

Why this module exists
----------------------
``ClawHubAdapter._map`` (``federation_adapters.py``) resolves an owner handle for
every row it maps::

    owner = row.get("ownerHandle") or clawhub_url.resolve_owner(str(slug))

``resolve_owner`` issues a live ``GET clawhub.ai/api/search?q=<slug>`` for any
slug not already in its process-local ``_OWNER_CACHE``. ``_map`` runs once per
row, so an N-row ClawHub page fires up to **N sequential upstream HTTP calls**.

Measured against live prod, ``/api/skills/external?sources=<one>&limit=24``:

===================  =========
source               latency
===================  =========
well-known           0.27 s
github-oss           0.30 s
lobehub              0.53 s
hermes-hub           0.57 s
skills-sh            1.51 s
browse-sh            1.78 s
**clawhub (cold)**   **59 s** (2026-07-26); **>90 s / timeout** (2026-07-30)
clawhub (warm)       0.34 s
===================  =========

All six non-clawhub sources together: 0.62 s. Warm ClawHub is 0.34 s — the
upstream is healthy. Our per-row fetch pattern is the defect, and it is getting
worse, not better.

The fix, and why it is not a new resolver
-----------------------------------------
The durable mapping **already exists and was already being computed**:

* ``FederationHubSkill.owner_handle`` — persisted, populated by the sp2607_0
  backfill migration.
* ``hub_owner_carry.load_resolved_owner_handles(db)`` — reads the whole
  ``identifier -> owner_handle`` map in ONE query, with safety re-validation.

Both were wired only into ``hub_snapshot.py`` (the reindex/ingest path), never
into the live per-request path. So this module adds no new resolution logic; it
just calls the existing loader on the request thread and seeds the cache the
adapter already consults.

Threading note
--------------
The metasearch fan-out queries sources in worker threads. Rather than thread a
DB session down into those threads (which would mean touching ``SourceAdapter``,
``_query_one_source`` and the sequential path's signatures), this primes the
**process-local** cache from the request thread *before* the fan-out starts. The
workers then hit a warm dict. Same effect, no session-lifetime hazard.

Failure policy
--------------
Best-effort, always. A priming failure must degrade to today's behaviour (slow
live lookups), never break search — search is the top-of-funnel read path for
every user.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Prime at most once per process per TTL window. The snapshot changes only on
#: reindex, so re-reading it on every request would add a pointless query to the
#: hot path. ``None`` means "never primed".
_LAST_PRIMED_AT: float | None = None

#: Re-prime interval. Comfortably shorter than the reindex cadence so freshly
#: backfilled owners land, comfortably longer than a burst of search traffic.
_PRIME_TTL_SECONDS = 900.0


def prime_clawhub_owner_cache(db: "Session", *, force: bool = False) -> int:
    """Seed the ClawHub owner cache from the persisted snapshot. Best-effort.

    Returns the number of cache entries added (0 if skipped or on failure).
    Never raises — see the module docstring's failure policy.
    """
    global _LAST_PRIMED_AT

    import time

    now = time.monotonic()
    if not force and _LAST_PRIMED_AT is not None and (now - _LAST_PRIMED_AT) < _PRIME_TTL_SECONDS:
        return 0

    try:
        from app.services import clawhub_url
        from app.services.hub_owner_carry import load_resolved_owner_handles

        resolved = load_resolved_owner_handles(db)
        added = clawhub_url.prime_owner_cache(resolved)
        # Set the stamp only on success, so a transient DB error retries next
        # request rather than silently disabling priming for the whole TTL.
        _LAST_PRIMED_AT = now
        if added:
            logger.info("clawhub owner cache primed: +%d entries (%d in snapshot)", added, len(resolved))
        return added
    # Rationale: priming is a best-effort optimisation on the top-of-funnel
    # search read path. Any failure must degrade to live per-row lookups (today's
    # behaviour), never surface as a 500 on search.
    except Exception:  # noqa: BLE001
        logger.warning("clawhub owner cache priming failed; falling back to live lookups", exc_info=True)
        return 0


def _reset_for_tests() -> None:
    """Clear the TTL stamp so a test can prime deterministically."""
    global _LAST_PRIMED_AT
    _LAST_PRIMED_AT = None
