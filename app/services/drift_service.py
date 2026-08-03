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

  resolve_bundle_entries(db, bundle_id) -> list[entry]
      THE single resolution authority (converge_0208 P1). Every consumer —
      mint, reconcile, the backfill script — resolves a bundle's declared
      membership through this one function, so the pin-vs-track semantics
      cannot diverge between call sites again.

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
import logging
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Bundle, BundleLock, BundleSkill, Skill, SkillVersion

logger = logging.getLogger(__name__)

# Membership sources that mean "NOT part of the declared desired state".
# Mirrors reconcile._UNDECLARED_SOURCES — a removed skill is soft-deleted via
# source='disabled' (reconcile-contract §1). The lock is the desired state, so
# a disabled row must never be frozen into it: it would resurrect a removed
# skill on every member and force mint to vouch for an artifact nobody installs.
UNDECLARED_SOURCES = {"disabled"}


class LockMintError(RuntimeError):
    """A bundle entry resolves to something no member could actually install.

    converge_0208 P1. Raised at MINT time — loudly, once, naming the entry —
    instead of letting the entry reach an agent that fetches a 404 and rolls
    the whole apply back on a 30-minute loop.
    """

    def __init__(self, *, bundle_name: str, slug: str, version: str | None, reason: str) -> None:
        self.bundle_name = bundle_name
        self.slug = slug
        self.version = version
        self.reason = reason
        shown = f"{slug} {version}" if version else slug
        super().__init__(
            f"cannot mint lock for bundle {bundle_name}: {shown} has no resolvable artifact ({reason})"
        )


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


def _resolve_entry_snapshot(
    db: Session, bs: BundleSkill
) -> tuple[dict[str, Any], Skill | None, SkillVersion | None]:
    """Resolve one bundle-skill membership row to a frozen lock entry.

    Picks the version to freeze:
      - pin_mode='pin' with pinned_version → that exact version.
      - otherwise the skill's latest version (the 'track' current head).

    ``pin_mode`` — not ``pinned_version`` — decides. A 'track' row that carries
    a ``pinned_version`` is carrying reconcile-apply bookkeeping residue, not an
    owner's pin; honouring it is exactly the bug converge_0208 P1 closes. The
    only writer of an owner's intent is the pin route, which now sets both
    columns together.

    Returns the entry plus the rows it resolved from, so the caller can judge
    artifact resolvability without re-querying.
    """
    if bs.skill_id is None:
        # spotify_2607 A — a federated-only track. LoopSkill hosts no artifact
        # for it; the agent fetches from the origin registry, so there is no
        # local version row to resolve and nothing local to dangle.
        return (
            {
                "slug": bs.federated_slug or str(bs.id),
                "version": bs.pinned_version,
                "content_hash": None,
                "source": bs.federated_source or "federated",
                "pin_mode": bs.pin_mode or "track",
            },
            None,
            None,
        )

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
        # SEMANTIC latest, not created_at order: reconcile has resolved the head
        # with semver_key since portal_0610 B2, and the two resolvers must not
        # disagree — max("1.9.0", "1.10.0") is 1.10.0 under one ordering and
        # 1.9.0 under a publish-time ordering. (skill_id, semver) is unique, so
        # semver_key plus the row id is already a total order; created_at is
        # deliberately NOT in the key — it is nullable and inconsistently
        # tz-aware across writers, which would make the comparison itself fail.
        from app.services.semver import semver_key

        candidates = db.query(SkillVersion).filter(SkillVersion.skill_id == bs.skill_id).all()
        if candidates:
            version_row = max(candidates, key=lambda v: (semver_key(v.semver), str(v.id)))

    version = version_row.semver if version_row else (bs.pinned_version or "unknown")
    content_hash = version_row.checksum_sha256 if version_row else None
    return (
        {
            "slug": slug,
            "version": version,
            "content_hash": content_hash,
            "source": "local",
            "pin_mode": bs.pin_mode or "track",
        },
        skill,
        version_row,
    )


def _is_unpublished(bs: BundleSkill, skill: Skill | None, version_row: SkillVersion | None) -> bool:
    """True for a local, non-federated entry that has no published version yet."""
    if version_row is not None or bs.skill_id is None or bs.federated_source:
        return False

    from app.services.bundle_external import is_external_skill

    # A materialized federation pointer legitimately has no local version row —
    # its bytes come from the origin registry.
    return not is_external_skill(skill)


def resolve_bundle_entries(db: Session, bundle_id, *, strict: bool = False) -> list[dict[str, Any]]:
    """THE resolution authority: a bundle's declared membership → lock entries.

    Every consumer goes through here — mint, reconcile, the backfill script —
    so there is exactly ONE place that decides what a member installs and the
    pin-vs-track divergence cannot reappear.

    ``strict=True`` raises :class:`LockMintError` for the first entry whose
    resolved version has no reachable artifact. ``strict=False`` resolves the
    same way but vouches for nothing; reconcile uses it as its never-take-a-
    bundle-offline fallback when a lock cannot be minted.
    """
    from app.services import artifact_resolution

    bundle = db.query(Bundle).filter(Bundle.id == bundle_id).first()
    bundle_name = getattr(bundle, "name", None) or str(bundle_id)

    members = (
        db.query(BundleSkill)
        .filter(BundleSkill.bundle_id == bundle_id)
        .order_by(BundleSkill.install_order, BundleSkill.added_at)
        .all()
    )

    entries: list[dict[str, Any]] = []
    for bs in members:
        if bs.source in UNDECLARED_SOURCES:
            continue
        entry, skill, version_row = _resolve_entry_snapshot(db, bs)

        if _is_unpublished(bs, skill, version_row):
            # A local skill with no version at all is not a broken artifact —
            # it is a draft that has never been published, so there is nothing
            # to freeze and nothing a member could install. It is skipped, not
            # refused: adding a skill to a bundle before its first publish is a
            # normal authoring order, and refusing it would ban that.
            #
            # Skipping is also strictly safer than the old behaviour. Reconcile
            # used to emit an add row with version=None for these, which
            # _attach_tarball_urls cannot sign a URL for — and the client rolls
            # the ENTIRE apply back when a diff row has no tarball_url. So a
            # single unpublished member used to take the whole bundle down.
            logger.info(
                "bundle-lock: skipping unpublished entry %s in bundle %s",
                entry["slug"],
                bundle_name,
            )
            continue

        if strict:
            reason = artifact_resolution.unresolvable_reason(
                db,
                skill=skill,
                semver=entry["version"],
                version_row=version_row,
                federated_source=bs.federated_source,
            )
            if reason is not None:
                raise LockMintError(
                    bundle_name=bundle_name,
                    slug=entry["slug"],
                    version=entry["version"] if version_row is not None else None,
                    reason=reason,
                )
        entries.append(entry)
    return entries


def mint_bundle_lock(db: Session, bundle: Bundle, created_by=None) -> BundleLock:
    """Freeze the bundle's current membership into a NEW immutable lock revision.

    Immutability: never mutates an existing BundleLock. revision = prev + 1.
    Idempotency is the caller's concern — ``bundle_lock_sync.sync_bundle_lock``
    is the idempotent wrapper every mutation path uses.

    Raises :class:`LockMintError`, without writing anything, when an entry
    resolves to a version no member could install. That refusal IS the feature:
    one loud publish-time error in place of a silent 30-minute rollback loop.
    """
    entries = resolve_bundle_entries(db, bundle.id, strict=True)
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
