"""Change-driven Hermes Hub snapshot sync (fed1004).

The Hub publishes its whole federated index (~100k skills, ~41 MB) as one JSON
file on GitHub Pages and rebuilds it several times a day (2026-10-04: generated
09:24 UTC, Pages deploy 13:56 UTC). The nightly 03:00 reindex was the only
ingest, so the local index — the MCP search floor, the ``hermes-hub`` metasearch
source, the bundle resolver — lagged upstream by up to 24 hours.

The Pages response carries a strong ``ETag`` (and ``Cache-Control:
max-age=600``). This module sends ``If-None-Match`` with the ETag of the last
successful ingest:

- ``304`` → nothing changed upstream. ``walked_at`` is bumped (the index IS
  current — the freshness predicate must say so) and no row is touched. Cost:
  one request, no body.
- ``200`` → the body is handed to ``ingest_hub_snapshot`` through an injected
  getter (no second download). The ingest is one transaction — delete, insert,
  cache-row write, one commit — so readers see the old index until the new one
  is complete. The new ETag is stored only when the ingest succeeded.
- anything else → an error report; the index and the stored ETag stay as they
  are, so the next run retries.

The nightly run stays a FORCED full ingest: our own mapping code (for example
the origin-URL derivation) can change while upstream does not, and only a full
ingest re-applies it to the stored rows.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.models import FederationIndexCache

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

HUB_SOURCE_ID = "hermes-hub"
MAX_ETAG_LEN = 256


def _report_from_row(row: FederationIndexCache, status: str, etag: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "indexed": row.indexed_count,
        "deduped": row.deduped_indexed_count,
        "installable": row.installable_count,
        "etag": etag,
    }


def sync_hub_snapshot(db: "Session", *, commit: bool = True, _get: Any = None) -> dict[str, Any]:
    """Ingest the Hub snapshot only when upstream changed. Returns a report with
    ``status`` in ``unchanged`` | ``ok`` | ``error``."""
    from app.services.federation_fetch import guarded_get
    from app.services.hub_snapshot import HUB_FETCH_TIMEOUT_S, HUB_SNAPSHOT_URL, ingest_hub_snapshot

    get = _get or guarded_get
    row = db.get(FederationIndexCache, HUB_SOURCE_ID)
    etag = (row.upstream_etag if row is not None else None) or None
    # A stored ETag without a successful ingest behind it must never turn a
    # 304 into "current": only trust it when the row holds a real count.
    trusted = etag if (row is not None and row.indexed_count) else None
    headers = {"If-None-Match": trusted} if trusted else None

    try:
        resp = get(HUB_SNAPSHOT_URL, timeout=HUB_FETCH_TIMEOUT_S, headers=headers)
    # Rationale: a network failure is a retry next hour, never a crash.
    except Exception as exc:  # noqa: BLE001
        logger.warning("hub sync: conditional fetch failed: %s", exc)
        return {"status": "error", "indexed": None, "error": f"fetch failed: {exc}"[:200]}

    status = getattr(resp, "status_code", None)
    if status == 304 and trusted and row is not None:
        row.walked_at = datetime.now(timezone.utc)
        row.last_error = None
        db.flush()
        if commit:
            db.commit()
        logger.info("hub sync: upstream unchanged (etag %s)", trusted)
        return _report_from_row(row, "unchanged", trusted)
    if status != 200:
        logger.warning("hub sync: unexpected status %s; index kept", status)
        return {"status": "error", "indexed": None, "error": f"unexpected status {status}"}

    report = ingest_hub_snapshot(db, url=HUB_SNAPSHOT_URL, _get=lambda *a, **k: resp, commit=False)
    if report.get("status") == "ok":
        new_etag = ((getattr(resp, "headers", None) or {}).get("etag") or "").strip()[:MAX_ETAG_LEN] or None
        fresh_row = db.get(FederationIndexCache, HUB_SOURCE_ID)
        if fresh_row is not None:
            fresh_row.upstream_etag = new_etag
        report["etag"] = new_etag
    if commit:
        db.commit()
    return report
