"""Persistent federation index-cache layer — superset_0606 Phase B.

The storage backbone the depth adapters fill and the ``/api/skills/external``
route reads from. A cold page load reads counts + first-page from the
``federation_index_cache`` table (one DB row per source); it NEVER triggers an
inline cursor/sitemap walk. The expensive walks run in the background reindex
cron (``recipes-federation-reindex``) which calls ``write_source_cache`` per
source after walking its adapter.

Honest-count discipline (decision #5, enforced here):
  - ``indexed_count`` is everything discovered; ``installable_count`` is the
    resolved redistributable subset — never conflated, never fabricated.
  - A source that failed its last walk keeps ``indexed_count = NULL``; the
    route's sum-of-indexed OMITS null sources rather than inventing a number.
  - ``stale`` is derived deterministically from ``walked_at + ttl_seconds`` vs
    now — a timestamped, honest freshness signal, never hidden.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.models import FederationIndexCache

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Default staleness windows (seconds). The giants walk daily; cheap catalogs
# refresh hourly. The reindex cron passes an explicit ttl per source.
TTL_DAILY = 86_400
TTL_HOURLY = 3_600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_stale(walked_at: datetime | None, ttl_seconds: int) -> bool:
    """True when the cache row is older than its TTL (or never walked)."""
    if walked_at is None:
        return True
    # Normalize naive timestamps (SQLite test rows) to UTC for comparison.
    if walked_at.tzinfo is None:
        walked_at = walked_at.replace(tzinfo=timezone.utc)
    return (_now() - walked_at) > timedelta(seconds=max(ttl_seconds, 0))


def read_source_cache(db: "Session", source: str) -> dict[str, Any] | None:
    """Read one source's cached block, or None if never cached.

    Returns a dict matching the route's per_source block shape, plus the
    honest freshness fields:
      {indexed, installable, walked_at, stale, ttl_seconds, last_error}
    ``indexed``/``installable`` may be None (source never successfully walked) —
    the caller must treat None as "omit from sum", never as 0.
    """
    row = db.get(FederationIndexCache, source)
    if row is None:
        return None
    walked_at_iso = row.walked_at.isoformat() if row.walked_at else None
    return {
        "indexed": row.indexed_count,
        "installable": row.installable_count,
        "walked_at": walked_at_iso,
        "stale": _is_stale(row.walked_at, row.ttl_seconds),
        "ttl_seconds": row.ttl_seconds,
        "last_error": row.last_error,
    }


def read_first_page(db: "Session", source: str) -> list[dict[str, Any]]:
    """Read the cached first page of results for a source (empty list if none)."""
    row = db.get(FederationIndexCache, source)
    if row is None or not isinstance(row.first_page, list):
        return []
    return row.first_page


def write_source_cache(
    db: "Session",
    source: str,
    *,
    indexed_count: int | None,
    installable_count: int | None,
    first_page: list[dict[str, Any]] | None = None,
    ttl_seconds: int = TTL_DAILY,
    last_error: str | None = None,
    commit: bool = True,
) -> FederationIndexCache:
    """Upsert one source's cache row (called by the reindex cron).

    Honest-count guard: ``installable_count`` is clamped to never exceed
    ``indexed_count`` when both are present — installable is a SUBSET of indexed
    by definition (decision #5), so a walker bug that reported more installable
    than indexed is corrected here rather than surfaced as a fabricated number.
    A failed walk passes ``indexed_count=None`` + ``last_error`` and preserves
    the prior ``first_page`` so the route degrades to "stale" not "empty".
    """
    if indexed_count is not None and installable_count is not None and installable_count > indexed_count:
        logger.warning(
            "federation cache: installable(%s) > indexed(%s) for %s — clamping",
            installable_count,
            indexed_count,
            source,
        )
        installable_count = indexed_count

    row = db.get(FederationIndexCache, source)
    if row is None:
        row = FederationIndexCache(source=source)
        db.add(row)

    row.indexed_count = indexed_count
    row.installable_count = installable_count
    row.ttl_seconds = ttl_seconds
    row.last_error = last_error
    # Only advance walked_at + first_page on a SUCCESSFUL walk (indexed not None).
    if indexed_count is not None:
        row.walked_at = _now()
        if first_page is not None:
            row.first_page = first_page
    db.flush()
    if commit:
        db.commit()
    return row


def read_all_cached(db: "Session") -> dict[str, dict[str, Any]]:
    """Read every cached source block, keyed by source id."""
    out: dict[str, dict[str, Any]] = {}
    for row in db.query(FederationIndexCache).all():
        walked_at_iso = row.walked_at.isoformat() if row.walked_at else None
        out[row.source] = {
            "indexed": row.indexed_count,
            "installable": row.installable_count,
            "walked_at": walked_at_iso,
            "stale": _is_stale(row.walked_at, row.ttl_seconds),
            "ttl_seconds": row.ttl_seconds,
            "last_error": row.last_error,
        }
    return out


def sum_indexed(blocks: dict[str, dict[str, Any]]) -> int:
    """Sum the indexed counts across source blocks, OMITTING null sources.

    decision #5: a null/failed source is omitted from the sum, never counted
    as 0 and never fabricated. This is the dual-count the portal header reads.
    """
    return sum(b["indexed"] for b in blocks.values() if isinstance(b.get("indexed"), int))


def sum_installable(blocks: dict[str, dict[str, Any]]) -> int:
    """Sum the installable counts across source blocks, OMITTING null sources."""
    return sum(b["installable"] for b in blocks.values() if isinstance(b.get("installable"), int))
