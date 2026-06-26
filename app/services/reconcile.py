"""Reconcile engine — evergreen_0206 Phase B.

Extends the update-only `recipes_sync` into a full desired-state reconcile that
computes the complete diff between a cookbook's *declared* skill set (server
desired-state) and an agent's *reported* local set (its lockfile):

    {add: [...], update: [...], remove: [...], drift: [...]}

See docs/reconcile-contract.md §1 for the canonical shape.

Design:
  * Pure-ish compute (`compute_reconcile_plan`) — given the cookbook's declared
    rows + the caller's reported local state, returns the diff. No DB writes.
  * `plan` (dry_run) returns the diff; `apply` executes the server-side pin
    writes (same as recipes_sync) and returns the diff the *client* must apply
    locally (install_urls for add/update/drift, uninstall directives for
    remove). The agent-side application is Phase D.
  * REMOVE is gated behind an explicit `prune` flag (premortem #4 — default
    reconcile never uninstalls). REMOVE keys off the cookbook no longer
    declaring the skill (row absent OR source='disabled').
  * Backward-compat: `recipes_sync` is untouched for existing callers; this is a
    new surface (`recipes_reconcile`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.models import Bundle, BundleSkill, Skill, SkillVersion

# Sources that mean "this skill is NOT part of the declared desired state".
# A removed skill is soft-deleted via source='disabled' (no removed_at column —
# reconcile-contract §1). Anything else is declared.
_UNDECLARED_SOURCES = {"disabled"}


@dataclass(frozen=True)
class LocalSkillState:
    """One skill as reported by the agent's local lockfile."""

    slug: str
    pinned_version: str | None = None
    sha256: str | None = None


@dataclass
class ReconcilePlan:
    """The four-way diff. Lists hold plain dicts (JSON-serialisable)."""

    add: list[dict[str, Any]] = field(default_factory=list)
    update: list[dict[str, Any]] = field(default_factory=list)
    remove: list[dict[str, Any]] = field(default_factory=list)
    drift: list[dict[str, Any]] = field(default_factory=list)

    @property
    def no_op(self) -> bool:
        return not (self.add or self.update or self.remove or self.drift)

    def to_dict(self) -> dict[str, Any]:
        return {
            "add": self.add,
            "update": self.update,
            "remove": self.remove,
            "drift": self.drift,
        }


def _declared_skills(db: Session, cookbook_id: UUID) -> dict[str, dict[str, Any]]:
    """Return {slug: {skill_id, pinned_version, latest_semver, latest_sha256}}
    for every skill the cookbook DECLARES (source != disabled).
    """
    # portal_0610 B2 — SEMANTIC latest per skill, not lexicographic
    # func.max(semver). Fetch the declared rows, then resolve each skill's
    # latest version semantically in Python.
    from app.services.semver import latest_semver_for_skills

    rows = (
        db.query(
            BundleSkill.skill_id,
            Skill.slug,
            BundleSkill.pinned_version,
            BundleSkill.source,
        )
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == cookbook_id)  # compat-alias
        .all()
    )

    latest_by_skill = latest_semver_for_skills(db, {r.skill_id for r in rows})

    declared: dict[str, dict[str, Any]] = {}
    for r in rows:
        if r.source in _UNDECLARED_SOURCES:
            continue
        latest_semver = latest_by_skill.get(r.skill_id)
        # The "target" version is the pin if set, else the latest published.
        target = r.pinned_version or latest_semver
        declared[r.slug] = {
            "skill_id": r.skill_id,
            "slug": r.slug,
            "pinned_version": r.pinned_version,
            "target_version": target,
            "latest_semver": latest_semver,
        }
    return declared


def _sha_for(db: Session, skill_id: UUID, semver: str | None) -> str | None:
    """checksum_sha256 for a specific (skill, semver), or None."""
    if semver is None:
        return None
    v = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill_id, SkillVersion.semver == semver)
        .first()
    )
    return v.checksum_sha256 if v else None


def compute_reconcile_plan(
    db: Session,
    cookbook_id: UUID,
    local: list[LocalSkillState],
    *,
    prune: bool = False,
) -> ReconcilePlan:
    """Compute the four-way reconcile diff.

    Args:
        cookbook_id: the desired-state cookbook.
        local: the agent's reported local skill set (from its lockfile).
        prune: when True, skills present locally but no longer declared are
            emitted in `remove`. When False (default), remove is always empty —
            reconcile never auto-uninstalls (premortem #4).
    """
    declared = _declared_skills(db, cookbook_id)
    local_by_slug = {ls.slug: ls for ls in local}

    plan = ReconcilePlan()

    for slug, d in declared.items():
        target = d["target_version"]
        target_sha = _sha_for(db, d["skill_id"], target)
        ls = local_by_slug.get(slug)

        if ls is None:
            # Declared but absent locally → ADD.
            plan.add.append({"slug": slug, "version": target, "checksum_sha256": target_sha})
            continue

        if ls.pinned_version != target:
            # Present at a different version → UPDATE.
            plan.update.append(
                {
                    "slug": slug,
                    "from": ls.pinned_version,
                    "to": target,
                    "checksum_sha256": target_sha,
                }
            )
            continue

        # Right version but content mismatch → DRIFT (corrupted / hand-edited).
        if target_sha is not None and ls.sha256 is not None and ls.sha256 != target_sha:
            plan.drift.append({"slug": slug, "expected_sha256": target_sha, "current_sha256": ls.sha256})

    if prune:
        declared_slugs = set(declared)
        for slug, ls in local_by_slug.items():
            if slug not in declared_slugs:
                plan.remove.append({"slug": slug})

    return plan


def recipes_reconcile(
    db: Session,
    *,
    cookbook_id: str,
    local: list[dict[str, Any]] | None = None,
    prune: bool = False,
    dry_run: bool = False,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Full desired-state reconcile (Phase B).

    plan (dry_run=True) → returns the diff only.
    apply (dry_run=False, default per Adam 2026-05-07) → executes server-side
    pin writes for update rows + advances the generation token, returns the diff
    the client must apply locally.

    `local` is the caller's reported lockfile state: a list of
    {slug, pinned_version, sha256}. Omitted → treated as empty (everything in
    the cookbook is an ADD).
    """
    if ctx is None:
        ctx = AuthContext(scope="master")

    try:
        cb_uuid = UUID(cookbook_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_cookbook_id", "cookbook_id": cookbook_id}

    cb = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    if not cb:
        return {"error": "not_found", "cookbook_id": cookbook_id}

    # TENANT ISOLATION (reconcile-contract §7): ownership precedes any diff.
    if not authz.can_write_cookbook(ctx, cb):
        return {"error": "cookbook_forbidden", "cookbook_id": cookbook_id}

    local_states = [
        LocalSkillState(
            slug=item["slug"],
            pinned_version=item.get("pinned_version"),
            sha256=item.get("sha256"),
        )
        for item in (local or [])
    ]

    plan = compute_reconcile_plan(db, cb_uuid, local_states, prune=prune)

    result: dict[str, Any] = {
        "cookbook_id": cookbook_id,
        "generation": cb.updated_at.isoformat() if cb.updated_at else None,
        "diff": plan.to_dict(),
        "no_op": plan.no_op,
    }

    if dry_run:
        return result

    # ── APPLY path ───────────────────────────────────────────────────────
    # Server-side, apply means: advance any pin that UPDATE rows imply, then
    # bump the generation token. The agent-side application (pull + atomic swap)
    # is Phase D; here we only persist the server's desired-state bookkeeping.
    mutated = False
    for upd in plan.update:
        slug = upd["slug"]
        to_version = upd["to"]
        skill = db.query(Skill).filter(Skill.slug == slug).first()
        if skill is None:
            continue
        db.query(BundleSkill).filter(
            BundleSkill.bundle_id == cb_uuid,  # compat-alias
            BundleSkill.skill_id == skill.id,
        ).update({"pinned_version": to_version})
        mutated = True

    if mutated:
        db.query(Bundle).filter(Bundle.id == cb_uuid).update(
            {"updated_at": func.now()}, synchronize_session=False
        )

    db.commit()
    result["applied"] = True
    return result
