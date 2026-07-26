"""ClawHub owner-handle resolution for Hub snapshot rows, and its durability.

WHY THIS MODULE EXISTS
----------------------
``hub_snapshot.bulk_upsert_skills`` implements the Hub ingest as
``DELETE FROM federation_hub_skills`` followed by a bulk re-insert from the
upstream snapshot — the snapshot is the source of truth, so this is correct and
idempotent for everything the snapshot actually carries.

It does not carry the ClawHub owner handle. ClawHub skill pages are
owner-scoped (``/<owner>/skills/<slug>``) but the Hub snapshot has no owner
field, so the handle only exists because we resolved it out-of-band (issue
#141, ``scripts/spotify_2607_clawhub_owner_backfill.py``).

Without the carry-forward here, the 03:00 ``federation_reindex`` cron would
delete all 69,150 resolved handles every night and revert three quarters of the
federated index to its browse-page fallback — silently, with every link still
answering HTTP 200 the whole time. That is the same soft-failure shape as the
bug being fixed, so it is guarded by an explicit RED-provable test rather than
a comment (``TestOwnerHandleSurvivesReindex`` in
``tests/test_spotify_2607_clawhub_owner_backfill.py``).

Split out of ``hub_snapshot`` to keep that module under the 600-line
god-object threshold enforced by ``test_w0_2_pyfile_size_discipline``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.models import FederationHubSkill
from app.services.clawhub_url import clawhub_skill_url, is_safe_token

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def owner_handle_for_row(row: dict[str, Any]) -> str | None:
    """Best-effort ClawHub owner handle from a Hub snapshot row.

    Today's snapshot carries no owner (verified 2026-07-26 against the live
    index: ``extra`` is empty and 0 of 69,150 clawhub identifiers contain a
    ``/``), so this normally returns ``None`` and callers degrade to the browse
    page — a working link — while the resolved value arrives via the backfill.

    It reads the fields upstream would plausibly use IF it ever starts shipping
    the handle, so this self-upgrades the moment that happens instead of
    silently keeping the fallback forever. Same posture as ``resolved_repo_path``:
    prefer a resolved field when present, fail closed when it is not.
    """
    for key in ("owner", "owner_handle", "ownerHandle", "handle", "namespace"):
        value = row.get(key)
        if isinstance(value, dict):
            value = value.get("handle")
        if isinstance(value, str) and is_safe_token(value):
            return value.strip()
    # ``owner/slug`` packed into the identifier is the other shape upstream
    # could adopt; accept it defensively (both halves must be safe tokens).
    identifier = (row.get("identifier") or "").strip()
    if identifier.count("/") == 1:
        owner, _, leaf = identifier.partition("/")
        if is_safe_token(owner) and is_safe_token(leaf):
            return owner
    return None


def load_resolved_owner_handles(db: "Session") -> dict[str, str]:
    """Read the persisted ClawHub ``identifier -> owner_handle`` map.

    MUST be called BEFORE the ingest's delete — afterwards the mapping is gone.

    Keyed on ``identifier`` (upstream ClawHub's stable slug), NOT our internal
    ``slug``: ours is generated during dedupe and can shift between snapshots,
    so it is not a durable join key across a re-ingest.
    """
    from sqlalchemy import select

    rows = db.execute(
        select(FederationHubSkill.identifier, FederationHubSkill.owner_handle).where(
            FederationHubSkill.upstream_source == "clawhub",
            FederationHubSkill.owner_handle.isnot(None),
        )
    ).all()

    resolved: dict[str, str] = {}
    for identifier, owner in rows:
        # Re-validate on the way OUT of the database as well as on the way in.
        # A row could predate the current token rules or have been written by an
        # older, looser code path; a stale unsafe value must never be
        # interpolated into a URL we publish. (A dot-only handle, for instance,
        # collapses the URL back into the soft-404 bare form — see
        # ``clawhub_url._DOTS_ONLY``.)
        if isinstance(identifier, str) and isinstance(owner, str) and is_safe_token(owner):
            resolved[identifier.strip()] = owner.strip()
    return resolved


def apply_resolved_owners(rows: list[dict[str, Any]], resolved: dict[str, str]) -> int:
    """Re-attach known owner handles to freshly-parsed snapshot rows.

    Sets ``owner_handle`` and re-mints ``origin_url`` as the owner-scoped deep
    link. Only upgrades rows the snapshot could not resolve itself — if upstream
    ever starts shipping owners inline, that value is fresher than our cached
    resolution and wins.

    Returns the number of rows upgraded.
    """
    if not resolved:
        return 0

    upgraded = 0
    for row in rows:
        if row.get("upstream_source") != "clawhub":
            continue
        if row.get("owner_handle"):
            continue  # snapshot already knew better — do not overwrite
        identifier = (row.get("identifier") or "").strip()
        owner = resolved.get(identifier)
        if not owner:
            continue
        row["owner_handle"] = owner
        row["origin_url"] = clawhub_skill_url(identifier, owner)
        upgraded += 1
    return upgraded
