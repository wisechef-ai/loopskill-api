"""spotify_1507 Phase B — Drift Killer service layer.

The bundle-lock lifecycle + three-way drift computation. Kept separate from
the route modules so the correctness-critical logic has one home and one test
surface.

  mint_bundle_lock(db, bundle) -> BundleLock
      Freeze the exact (slug, version, content_hash) of every member skill of
      a bundle into a new immutable lock revision. Called on bundle publish
      and on every accepted upstream bump.

  compute_lock_hash(entries) -> str
      Deterministic sha256 over the canonical-sorted locked_entries. Two locks
      with the same member set + versions + hashes produce the same lock_hash,
      so a deploy can compare two agents in O(1).

  classify_entry_drift(installed, locked) -> str
      The three-way verdict for ONE skill on ONE agent:
        in-sync            — installed content_hash == lock content_hash
        drift(local-edit)  — installed hash != lock hash AND installed hash is
                             not a known newer lock revision (the agent's local
                             copy was hand-edited — SURFACE it, never clobber)
        behind(bundle-updated) — installed hash == an OLDER lock revision's hash
                             (the bundle moved forward; the agent will converge
                             on next reconcile)
        missing            — declared in lock, absent on agent
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Bundle, BundleLock, BundleSkill, Skill, SkillVersion


def compute_lock_hash(entries: list[dict[str, Any]]) -> str:
    """Deterministic sha256 over canonical-sorted lock entries.

    Sort by slug so entry order can't change the hash; serialize only the
    identity-bearing fields (slug, version, content_hash) so cosmetic metadata
    (pin_mode, source label) doesn't perturb the "are these the same bytes?"
    question the hash answers.
    """
    canonical = sorted(
        (
            {
                "slug": e.get("slug"),
                "version": e.get("version"),
                "content_hash": e.get("content_hash"),
            }
            for e in entries
        ),
        key=lambda e: e["slug"] or "",
    )
    blob = json.dumps(canonical, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _resolve_entry_snapshot(db: Session, bs: BundleSkill) -> dict[str, Any]:
    """Resolve one bundle-skill membership row to a frozen lock entry.

    Picks the version to freeze:
      - pin_mode='pin' with pinned_version → that exact version.
      - otherwise the skill's latest version (the 'track' current head).
    Reads content_hash from the matching SkillVersion.checksum_sha256.
    """
    skill = db.query(Skill).filter(Skill.id == bs.skill_id).first()
    slug = skill.slug if skill else str(bs.skill_id)

    version_row: SkillVersion | None = None
    if bs.pin_mode == "pin" and bs.pinned_version:
        version_row = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == bs.skill_id, SkillVersion.semver == bs.pinned_version)
            .first()
        )
    if version_row is None:
        # 'track' head, or a pin whose version row is missing → latest version.
        # Order by created_at then id as a deterministic tiebreaker: fast test
        # inserts (and same-second prod publishes) can share created_at, so a
        # bare created_at.desc() picks a non-deterministic row. id is a v4 uuid
        # but stable per-row, giving a total order that never flip-flops within
        # a single mint call.
        version_row = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == bs.skill_id)
            .order_by(SkillVersion.created_at.desc(), SkillVersion.id.desc())
            .first()
        )

    version = version_row.semver if version_row else (bs.pinned_version or "unknown")
    content_hash = version_row.checksum_sha256 if version_row else None
    return {
        "slug": slug,
        "version": version,
        "content_hash": content_hash,
        "source": "local",
        "pin_mode": bs.pin_mode or "track",
    }


def mint_bundle_lock(db: Session, bundle: Bundle, created_by=None) -> BundleLock:
    """Freeze the bundle's current membership into a NEW immutable lock revision.

    Immutability: never mutates an existing BundleLock. revision = prev + 1.
    Idempotency is the caller's concern (publish mints once per publish event).
    """
    members = (
        db.query(BundleSkill)
        .filter(BundleSkill.bundle_id == bundle.id)
        .order_by(BundleSkill.install_order, BundleSkill.added_at)
        .all()
    )
    entries = [_resolve_entry_snapshot(db, bs) for bs in members]
    lock_hash = compute_lock_hash(entries)

    prev_max = db.query(func.max(BundleLock.revision)).filter(BundleLock.bundle_id == bundle.id).scalar()
    next_rev = (prev_max or 0) + 1

    lock = BundleLock(
        bundle_id=bundle.id,
        revision=next_rev,
        locked_entries=entries,
        lock_hash=lock_hash,
        created_by=created_by,
    )
    db.add(lock)
    db.commit()
    db.refresh(lock)
    return lock


def current_lock(db: Session, bundle_id) -> BundleLock | None:
    """The latest (highest-revision) lock for a bundle, or None if never minted."""
    return (
        db.query(BundleLock)
        .filter(BundleLock.bundle_id == bundle_id)
        .order_by(BundleLock.revision.desc())
        .first()
    )


def classify_entry_drift(
    installed: dict[str, Any] | None,
    locked: dict[str, Any],
    known_hashes_by_rev: dict[int, set[str]] | None = None,
) -> str:
    """Three-way drift verdict for ONE skill on ONE agent.

    Args:
        installed: the agent's actual installed entry {version, checksum_sha256}
                   or None if the skill isn't installed.
        locked:    the current-lock entry {version, content_hash}.
        known_hashes_by_rev: optional {revision: {content_hash,...}} of PRIOR
                   lock revisions. When the installed hash matches an older
                   revision's hash, we can say 'behind' (bundle moved) vs
                   'drift' (genuine local edit).

    Returns one of: 'in-sync' | 'behind' | 'drift' | 'missing'.
    """
    if installed is None:
        return "missing"

    installed_hash = installed.get("checksum_sha256") or installed.get("content_hash")
    lock_hash = locked.get("content_hash")

    # No hash to compare (e.g. federated deep-link with no checksum) → fall back
    # to version-string comparison, treating equal versions as in-sync.
    if lock_hash is None or installed_hash is None:
        if installed.get("version") == locked.get("version"):
            return "in-sync"
        # version differs and we can't hash-verify → be conservative: behind
        # (the bundle declares a version the agent doesn't have) rather than
        # crying local-edit on unhashable content.
        return "behind"

    if installed_hash == lock_hash:
        return "in-sync"

    # Hash differs. Is the installed hash a KNOWN older revision of this bundle?
    # If so the agent is simply behind (bundle bumped, agent will converge).
    if known_hashes_by_rev:
        for _rev, hashes in known_hashes_by_rev.items():
            if installed_hash in hashes:
                return "behind"

    # Hash differs and matches no known lock revision → the agent's copy was
    # hand-edited locally. SURFACE it as drift; the reconcile must NOT clobber.
    return "drift"


def prior_revision_hashes(db: Session, bundle_id, slug: str) -> dict[int, set[str]]:
    """Map {revision: {content_hash for `slug` in that lock}} across ALL locks.

    Powers classify_entry_drift's behind-vs-drift decision: an installed hash
    that matches an older revision of the SAME slug means the agent is behind,
    not locally edited.
    """
    out: dict[int, set[str]] = {}
    locks = db.query(BundleLock).filter(BundleLock.bundle_id == bundle_id).all()
    for lock in locks:
        hashes: set[str] = set()
        for e in lock.locked_entries or []:
            if e.get("slug") == slug and e.get("content_hash"):
                hashes.add(e["content_hash"])
        if hashes:
            out[lock.revision] = hashes
    return out


def mark_compat_status(db: Session, skill: Skill, ok: bool) -> bool:
    """Flip a track's compat_status based on an upstream-resolve check result.

    ok=True  → 'active' (resolves & valid).
    ok=False → 'stale-upstream' (federated source 404'd / moved / schema-changed).

    Returns True when the status CHANGED (so the caller — the nightly compat
    cron — can emit a feed notice to followers of affected bundles only on the
    transition, not every run). Stamps compat_checked_at either way.
    """
    from datetime import UTC, datetime

    new_status = "active" if ok else "stale-upstream"
    changed = skill.compat_status != new_status
    skill.compat_status = new_status
    skill.compat_checked_at = datetime.now(UTC)
    db.commit()
    return changed
