"""converge_0208 P1 — keep the bundle lock in step with bundle mutations.

``BundleLock`` shipped in spotify_1507 as "THE core drift-killer primitive" and
then sat at zero rows in production for the whole of its life: nothing minted
it. Reconcile resolved installs off the raw membership row instead, so the lock
and the engine that was supposed to consume it never met.

This module is the join. Every mutation that changes what a member would
install — skill add, skill remove, pin change, pin-mode change, version publish
— calls in here, and reconcile resolves through what it writes.

Two entry points, differing only in who absorbs a refusal:

  sync_bundle_lock(db, bundle)
      Owner-initiated mutation. Mints iff something identity-bearing changed;
      on an unresolvable entry it rolls the mutation back and re-raises, so the
      owner gets one actionable 409 naming the slug rather than a bundle that
      is quietly un-deployable.

  try_sync_bundle_lock(db, bundle)
      Fan-out from an event the bundle's owner did not trigger (a publisher
      shipping a new version of a skill 14 bundles happen to track). Returns
      ``(lock, refusal_reason)`` and never raises or rolls back: one publisher
      must not be able to fail on another owner's broken pin, and a bundle that
      cannot re-mint simply keeps serving its previous — valid — lock revision.

Idempotency is by ``lock_hash``: identical (slug, version, content_hash) sets
produce an identical hash, so a no-op write mints nothing. That matters beyond
tidiness — ``prior_revision_hashes`` powers the behind-vs-drift verdict, and
padding it with duplicate revisions would make "the agent is on an older
revision" indistinguishable from "the agent hand-edited the file".
"""

from __future__ import annotations

import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Bundle, BundleLock, BundleSkill
from app.services.drift_service import (
    LockMintError,
    compute_lock_hash,
    current_lock,
    resolve_bundle_entries,
)

logger = logging.getLogger(__name__)


def _mint(db: Session, bundle: Bundle, entries: list[dict], lock_hash: str, created_by) -> BundleLock:
    """Append a new immutable revision. Never mutates an existing row."""
    prev_max = (
        db.query(func.max(BundleLock.revision)).filter(BundleLock.bundle_id == bundle.id).scalar()
    )
    lock = BundleLock(
        bundle_id=bundle.id,
        revision=(prev_max or 0) + 1,
        locked_entries=entries,
        lock_hash=lock_hash,
        created_by=created_by,
    )
    db.add(lock)
    db.flush()
    return lock


def sync_bundle_lock(db: Session, bundle: Bundle, *, created_by=None) -> BundleLock | None:
    """Mint a new lock revision iff the bundle's resolved entries changed.

    Returns the new lock, or ``None`` when nothing identity-bearing moved.
    Does not commit — the caller's transaction owns the mutation and the lock
    together, so a bundle can never be left mutated without a matching lock.

    Raises :class:`LockMintError` after rolling back, so the mutation that
    would have produced an uninstallable lock does not land.
    """
    db.flush()  # the caller's mutation must be visible to the resolver
    try:
        entries = resolve_bundle_entries(db, bundle.id, strict=True)
    except LockMintError:
        db.rollback()
        raise

    lock_hash = compute_lock_hash(entries)
    existing = current_lock(db, bundle.id)
    if existing is not None and existing.lock_hash == lock_hash:
        return None
    return _mint(db, bundle, entries, lock_hash, created_by)


def try_sync_bundle_lock(
    db: Session, bundle: Bundle, *, created_by=None, commit: bool = False
) -> tuple[BundleLock | None, str | None]:
    """Best-effort sync. Returns ``(lock_or_None, refusal_reason_or_None)``.

    Never raises and never rolls back. A refusal leaves the bundle's previous
    lock revision in place — stale but valid — which is strictly safer than
    minting a revision that points at bytes no agent can fetch.
    """
    db.flush()
    try:
        entries = resolve_bundle_entries(db, bundle.id, strict=True)
    except LockMintError as exc:
        logger.warning("bundle-lock refused for %s: %s", bundle.id, exc)
        return None, str(exc)

    lock_hash = compute_lock_hash(entries)
    existing = current_lock(db, bundle.id)
    if existing is not None and existing.lock_hash == lock_hash:
        return None, None
    lock = _mint(db, bundle, entries, lock_hash, created_by)
    if commit:
        db.commit()
    return lock, None


def resync_locks_for_skill(db: Session, skill_id) -> int:
    """Re-mint every bundle that declares ``skill_id`` after a version publish.

    A publish moves the head, so every 'track' entry pointing at that skill
    resolves somewhere new — that IS a desired-state change for those bundles,
    the same reasoning as ``reconcile.bump_declaring_bundles`` (which advances
    the generation token the agents poll on). Disabled rows are excluded: they
    are not desired state.

    Best-effort per bundle by design: a publisher must never be blocked by a
    broken entry in a bundle they do not own. Returns the number of bundles
    that actually got a new revision. Caller commits.
    """
    bundle_ids = [
        row[0]
        for row in db.query(BundleSkill.bundle_id)
        .filter(BundleSkill.skill_id == skill_id, BundleSkill.source != "disabled")
        .distinct()
        .all()
    ]
    if not bundle_ids:
        return 0

    minted = 0
    for bundle in db.query(Bundle).filter(Bundle.id.in_(bundle_ids)).all():
        lock, _reason = try_sync_bundle_lock(db, bundle)
        if lock is not None:
            minted += 1
    return minted


def locked_entries_for_reconcile(db: Session, bundle_id) -> list[dict]:
    """The declared entries reconcile must resolve from, lock-first.

    Order of preference:
      1. the bundle's current lock — the frozen, member-identical desired state
      2. mint-on-read — lazily freeze revision 1 for a bundle that has never
         been locked, so nothing breaks the moment this ships (all 14 live
         bundles are unlocked; a hard failure would take every one of them
         offline on deploy)
      3. in-memory resolution through the SAME resolver — used only when the
         lazy mint is refused, so a bundle with an unresolvable entry keeps
         reconciling exactly as it would have, rather than going dark. The
         resolver is shared, so there is still only one set of pin semantics.
    """
    lock = current_lock(db, bundle_id)
    if lock is not None:
        return list(lock.locked_entries or [])

    bundle = db.query(Bundle).filter(Bundle.id == bundle_id).first()
    if bundle is not None:
        lock, _reason = try_sync_bundle_lock(db, bundle, commit=True)
        if lock is not None:
            return list(lock.locked_entries or [])

    return resolve_bundle_entries(db, bundle_id, strict=False)
