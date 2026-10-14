"""loopskill_sync — PIN a bundle's skills to their current latest versions (or unpin).

Pin model (loopskill-api#405): an UNPINNED skill always installs the latest
version (``bundle_install._resolve_version``), so it never needs a sync. Sync
is the optional "hold every machine on the same versions" verb: it writes
``pinned_version`` = latest semver AND ``pin_mode='pin'`` for every skill whose
pin differs, so install and the lock/drift resolver (which decides on
``pin_mode``) serve the same version. It never touches the agent's files; to
update a machine, re-run ``loopskill_bundle_install``.

``unpin=True`` is the inverse: clears ``pinned_version`` and resets
``pin_mode='track'`` so the bundle follows the latest again.

Default behaviour (``dry_run=False``) applies; ``dry_run=True`` returns the
diff only — no DB mutations. Adam directive 2026-05-07: default
``dry_run=False`` is NON-NEGOTIABLE.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.models import Bundle, BundleSkill, Skill
from app.services.bundle_lock_sync import try_sync_bundle_lock

# BundleSkill.pin_mode vocabulary (models.BundleSkill, drift_service).
PIN = "pin"
TRACK = "track"


def _find_outdated_skills(db: Session, cookbook_id: UUID) -> list[dict[str, Any]]:
    """Return rows for skills in *cookbook_id* where pinned_version < latest.

    Each row dict has keys: skill_id, slug, pinned_version, latest.
    """
    # portal_0610 B2 — SEMANTIC latest per skill (SQL func.max(semver) is
    # lexicographic: max("1.9.0","1.10.0") wrongly returns "1.9.0", pinning
    # fleets to the OLDER version once a skill hits double-digit minors).
    from app.services.semver import latest_semver_for_skills

    declared = (
        db.query(
            BundleSkill.skill_id,
            Skill.slug,
            BundleSkill.pinned_version,
        )
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == cookbook_id)  # compat-alias
        .all()
    )

    latest_by_skill = latest_semver_for_skills(db, {r.skill_id for r in declared})

    out: list[dict[str, Any]] = []
    for r in declared:
        latest = latest_by_skill.get(r.skill_id)
        if latest is None:
            continue  # skill has no published version — nothing to advance to
        if r.pinned_version is None or r.pinned_version != latest:
            out.append(
                {
                    "skill_id": r.skill_id,
                    "slug": r.slug,
                    "from": r.pinned_version,
                    "to": latest,
                }
            )
    return out


def loopskill_sync(
    db: Session,
    *,
    cookbook_id: str,
    dry_run: bool = False,
    unpin: bool = False,
    caller: dict[str, Any] | None = None,  # kept for backwards compat; prefer ctx
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Pin a bundle's skills to their latest published versions (``unpin=True`` clears the pins).

    Phase B (Issue #15):
    - (a) Uses db.commit() instead of db.flush() so writes persist.
    - (b) Checks authz.can_write_cookbook(ctx, cb) before mutating.
    """
    # Resolve AuthContext: prefer ctx, fall back to master for legacy callers
    if ctx is None:
        ctx = AuthContext(scope="master")

    try:
        cb_uuid = UUID(cookbook_id)
    except (ValueError, AttributeError):
        return {
            "error": "invalid_bundle_id",
            "bundle_id": cookbook_id,  # canonical
            "cookbook_id": cookbook_id,  # compat-alias: legacy wire name
        }

    # Verify bundle exists
    cb = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    if not cb:
        return {
            "error": "not_found",
            "bundle_id": cookbook_id,  # canonical
            "cookbook_id": cookbook_id,  # compat-alias
        }

    # Phase B (Issue #15b): bundle ownership check.
    # mesh_0408 W1b (codex review of PR #202, finding 3): the answer is the SAME
    # `not_found` an absent id gets, four lines up. A distinct `cookbook_forbidden`
    # confirmed to an unauthorized caller that the UUID they hold is real — and
    # since one account owns every client org it runs, "unauthorized" here is
    # routinely a cross-tenant caller rather than a stranger.
    if not authz.can_write_cookbook(ctx, cb):
        return {
            "error": "not_found",
            "bundle_id": cookbook_id,  # canonical
            "cookbook_id": cookbook_id,  # compat-alias
        }

    if unpin:
        return _unpin(db, cb, cookbook_id, dry_run=dry_run, created_by=ctx.user_id)

    outdated = _find_outdated_skills(db, cb_uuid)

    if not outdated:
        return {
            "bundle_id": cookbook_id,  # canonical
            "cookbook_id": cookbook_id,  # compat-alias: legacy wire name
            "changes": [],
            "applied": not dry_run,
            "message": "All skills are up to date.",
        }

    changes = [
        {
            "slug": o["slug"],
            "from": o["from"],
            "to": o["to"],
            "action": "update",
        }
        for o in outdated
    ]

    result: dict[str, Any] = {
        "bundle_id": cookbook_id,  # canonical
        "cookbook_id": cookbook_id,  # compat-alias: legacy wire name
        "changes": changes,
    }

    if dry_run:
        return result

    # ── APPLY path (default) ─────────────────────────────────────────────
    for o in outdated:
        db.query(BundleSkill).filter(
            BundleSkill.bundle_id == cb_uuid,  # compat-alias
            BundleSkill.skill_id == o["skill_id"],
        ).update({"pinned_version": o["to"], "pin_mode": PIN})  # #405: never a half pin

    # evergreen_0206 Phase A: a pin-write changes the bundle's declared state,
    # so advance the generation token (Bundle.updated_at). SQLAlchemy onupdate
    # does NOT fire on child CookbookSkill writes — bump the parent explicitly so
    # the cheap-poll 304-fast-path (Phase D) stays truthful. Only on the apply
    # path with real outdated rows; a no-op sync returns early above and never
    # reaches here, so the token never falsely advances.
    db.query(Bundle).filter(Bundle.id == cb_uuid).update(
        {"updated_at": func.now()}, synchronize_session=False
    )

    # converge_0208: a pin change moves what members resolve — keep the lock in
    # step (best-effort: a refusal keeps the previous valid revision).
    try_sync_bundle_lock(db, cb, created_by=ctx.user_id)
    db.commit()  # Phase B (Issue #15a): commit, not flush

    # Build tarball URLs for the updated skills (same logic as loopskill_install)
    install_urls = _build_install_urls(db, outdated)
    result["applied"] = True
    if install_urls:
        result["install_urls"] = install_urls

    return result


def _unpin(db: Session, cb: Bundle, cookbook_id: str, *, dry_run: bool, created_by) -> dict[str, Any]:
    """Clear every pin in *cb* so its skills follow the latest again (#405).

    Same row scope as sync (local skills only). Honours ``dry_run``.
    """
    rows = (
        db.query(BundleSkill, Skill.slug)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == cb.id)  # compat-alias
        .filter((BundleSkill.pinned_version.isnot(None)) | (BundleSkill.pin_mode != TRACK))
        .all()
    )
    result: dict[str, Any] = {
        "bundle_id": cookbook_id,  # canonical
        "cookbook_id": cookbook_id,  # compat-alias: legacy wire name
        "changes": [
            {"slug": slug, "from": bs.pinned_version, "to": None, "action": "unpin"} for bs, slug in rows
        ],
    }
    if not rows:
        result.update(applied=not dry_run, message="No skills are pinned.")
        return result
    if dry_run:
        return result

    for bs, _slug in rows:
        bs.pinned_version = None
        bs.pin_mode = TRACK
    db.query(Bundle).filter(Bundle.id == cb.id).update({"updated_at": func.now()}, synchronize_session=False)
    try_sync_bundle_lock(db, cb, created_by=created_by)
    db.commit()
    result["applied"] = True
    return result


def _build_install_urls(db: Session, outdated: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return one-shot tarball URLs for each updated skill."""
    from itsdangerous import URLSafeTimedSerializer

    from app.config import settings  # noqa: F401

    from app import config

    try:
        # Issue #27 (secfix_1905/I-followup): salt MUST match install_routes._verify_signed_token.
        # Phase 3+4: primary salt changed to "loopskill-install"; verifier accepts both.
        serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="loopskill-install")
    # Rationale: URLSafeTimedSerializer init can fail if SIGNING_SECRET is empty; return empty list
    except Exception:  # noqa: BLE001
        return []

    public_origin = config.public_origin()

    urls: list[dict[str, str]] = []
    # Batched skill fetch (was N+1: one SELECT per row). One IN-query + dict
    # lookup, same pattern as _install_counts_for / search_skills (Issue #19).
    skill_ids = {o["skill_id"] for o in outdated if o.get("skill_id")}
    skills_by_id = (
        {s.id: s for s in db.query(Skill).filter(Skill.id.in_(skill_ids)).all()} if skill_ids else {}
    )
    for o in outdated:
        skill = skills_by_id.get(o["skill_id"])
        if not skill or not skill.versions:
            continue
        latest = skill.latest_version_row
        token = serializer.dumps({"slug": o["slug"], "version_id": str(latest.id), "mode": "files"})
        urls.append(
            {
                "slug": o["slug"],
                "version": o["to"],
                "tarball_url": (public_origin.rstrip("/") + "/api/skills/_download?token=" + token),
            }
        )

    return urls
