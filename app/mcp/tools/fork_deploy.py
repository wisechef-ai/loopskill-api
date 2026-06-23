"""recipes_tailor_version + recipes_cookbook_attach — close the MCP tailor loop.

loopclose_3005 Phase C.

Before this module the MCP fork flow stopped at fork-create (`recipes_tailor`):
agents could fork a public skill but had no MCP-native way to (a) upload a new
version, or (b) deploy a tailored fork into a cookbook so it installs like any
catalog skill. The two halves operated on disjoint tables — ``SkillFork`` /
``ForkVersion`` (fork tarballs) vs ``Skill`` / ``SkillVersion`` / ``CookbookSkill``
(the installable catalog unit). There was NO path, REST or MCP, bridging them.

This module adds the two missing MCP tools and the bridge between the tables:

  - ``recipes_tailor_version`` wraps POST /api/forks/{id}/version. MCP transport
    can't carry a multipart UploadFile, so the tarball is passed base64-encoded.
    Mints a ``ForkVersion`` and advances ``fork.latest_version_id``.

  - ``recipes_cookbook_attach`` is the bridge: it takes the fork's latest
    ForkVersion tarball, extracts its SKILL.md body, promotes it into a real
    catalog ``Skill`` (private, is_public=False) + ``CookbookSkill`` link via
    ``write_cookbook_skill``, AND mints a ``SkillVersion`` from the same tarball
    so ``recipes_cookbook_install`` can resolve an installable version. Because
    the promoted unit is a real ``Skill`` row, the install URL is signed with the
    canonical ``recipes-skill-install`` salt automatically (salt parity free) —
    a tailored fork now installs byte-identically to any catalog skill.

Result: tailor → tailor_version → cookbook_attach → cookbook_install is a
closed, MCP-native loop. ONE deployable unit (a Skill in a cookbook), one
install salt, no schema migration, no permanent dual-pathing.

Tier gate: Pro or above (matches forks_routes.require_operator). Master-key
callers are rejected — they have no user_id to own a fork or cookbook.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.config import settings
from app.models import (
    Cookbook,
    ForkVersion,
    Skill,
    SkillFork,
    SkillVersion,
)
from app.recipify import ValidationError, classify_skill, write_cookbook_skill
from app.tier_labels import _is_pro_tier

# ── Constants (mirrors forks_routes / publisher_routes; kept local so this
#    pure-MCP module doesn't import the fastapi-shaped route modules) ────────

MAX_TARBALL_BYTES = 10 * 1024 * 1024  # 10 MB — matches forks_routes/publisher
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.-]+)?$")
SKILL_MD_MAX_BYTES = 256 * 1024


def _forks_dir() -> Path:
    """Tarball storage root for forks. Mirror of forks_routes._forks_dir."""
    raw = (
        os.environ.get("RECIPES_FORKS_DIR")
        or getattr(settings, "RECIPES_FORKS_DIR", None)
        or str(Path(getattr(settings, "RECIPES_SKILLS_DIR", "/var/lib/recipes-skills")) / "forks")
    )
    return Path(raw)


def _skills_dir() -> Path:
    """Tarball storage root for catalog skills. Mirror of publisher_routes._skills_dir.

    Env var first so test patches (patch.dict os.environ) take effect, then
    settings (WR_ prefix), then default — identical precedence to the publisher.
    """
    return Path(
        os.environ.get("RECIPES_SKILLS_DIR")
        or getattr(settings, "RECIPES_SKILLS_DIR", None)
        or "/var/lib/recipes-skills"
    )


def _extract_skill_md(tarball_bytes: bytes) -> str | None:
    """Extract SKILL.md text from a tarball. Pure mirror of
    publisher_routes._extract_skill_md_from_tarball (no fastapi import).

    Returns the decoded text, or None if not found / unreadable. Bounded by
    256 KB to avoid memory blowup on a malicious tarball.
    """
    import io
    import tarfile

    try:
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as t:
            candidates: list[tarfile.TarInfo] = []
            for m in t.getmembers():
                if not m.isfile():
                    continue
                name = m.name.lstrip("./")
                parts = name.split("/")
                if parts[-1] != "SKILL.md":
                    continue
                if len(parts) <= 3:
                    candidates.append(m)
            if not candidates:
                return None
            candidates.sort(key=lambda m: len(m.name))
            chosen = candidates[0]
            if chosen.size > SKILL_MD_MAX_BYTES:
                return None
            f = t.extractfile(chosen)
            if f is None:
                return None
            raw = f.read(SKILL_MD_MAX_BYTES + 1)
            if len(raw) > SKILL_MD_MAX_BYTES:
                return None
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
    # Rationale: any tarball parse failure must surface as "no SKILL.md found"
    # rather than crash the MCP call — the caller maps None to a clean error.
    except Exception:  # noqa: BLE001
        return None


def _store_skill_tarball(slug: str, semver: str, tarball_bytes: bytes) -> str:
    """Write a tarball under {SKILLS_DIR}/{slug}/{semver}.tar.gz with a
    defense-in-depth traversal check. Returns the path string. Pure mirror of
    publisher_routes._store_tarball (returns str / raises ValueError instead of
    HTTPException so this module stays fastapi-free).
    """
    skills_root = _skills_dir().resolve()
    dest_dir = (skills_root / slug).resolve()
    if not str(dest_dir).startswith(str(skills_root) + os.sep):
        raise ValueError("path_traversal_detected")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{semver}.tar.gz"
    dest_path.write_bytes(tarball_bytes)
    try:
        dest_path.chmod(0o640)
    except OSError:
        pass  # some test runners (/tmp) reject chmod — nonfatal
    return str(dest_path)


def _require_pro_user(ctx: AuthContext | None) -> dict[str, Any] | None:
    """Return an error dict if the caller is not a Pro-tier user, else None.

    Master-key callers (user_id is None) are rejected: they own no forks.
    The tier check mirrors forks_routes.require_operator's Pro gate. Master
    scope is allowed through ONLY when it carries a user_id (it never does for
    the static admin key) — kept symmetric with the REST gate's master bypass.
    """
    if ctx is None or ctx.user_id is None:
        return {
            "error": "auth_required",
            "code": "auth_required",
            "message": "Must be authenticated as a user (not master key) to deploy forks.",
        }
    # master scope bypasses the tier check (parity with require_operator), but
    # a master key has user_id=None and is already rejected above. A user-scope
    # caller must hold Pro or above.
    if ctx.scope != "master" and not _is_pro_tier(ctx.tier):
        return {
            "error": "needs_tier",
            "code": "needs_tier",
            "message": "Tailoring forks requires the Pro tier or above.",
            "current_tier": ctx.tier,
        }
    return None


def _resolve_owned_fork(db: Session, ctx: AuthContext, fork_id: str) -> SkillFork | None:
    """Resolve a live fork owned by the caller, or None. Mirrors
    forks_routes._resolve_owned_fork (no-oracle 404 → None here)."""
    try:
        from uuid import UUID

        fid = UUID(str(fork_id))
    except (ValueError, TypeError):
        return None
    fork = db.query(SkillFork).filter(SkillFork.id == fid).first()
    if fork is None:
        return None
    if fork.user_id != ctx.user_id:
        return None
    if fork.visibility is None:  # soft-deleted
        return None
    return fork


def recipes_tailor_version(
    db: Session,
    *,
    fork_id: str,
    tarball_base64: str,
    semver: str,
    changelog: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Upload a new version tarball to a fork (MCP-native, base64 transport).

    Mirrors POST /api/forks/{fork_id}/version. The tarball is passed
    base64-encoded because MCP can't carry a multipart UploadFile. Validates
    semver + size, writes the tarball under the forks dir, mints a ForkVersion,
    and advances fork.latest_version_id.

    Tier: Pro or above. Returns the version metadata, or a structured error.
    """
    gate = _require_pro_user(ctx)
    if gate is not None:
        return gate
    assert ctx is not None  # narrowed by _require_pro_user

    fork = _resolve_owned_fork(db, ctx, fork_id)
    if fork is None:
        return {"error": "fork_not_found", "code": "fork_not_found"}

    if not SEMVER_RE.match(semver or ""):
        return {"error": "invalid_semver", "code": "invalid_semver", "semver": semver}

    try:
        tarball_bytes = base64.b64decode(tarball_base64 or "", validate=True)
    except (binascii.Error, ValueError):
        return {"error": "invalid_base64", "code": "invalid_base64"}

    size = len(tarball_bytes)
    if size == 0:
        return {"error": "empty_tarball", "code": "empty_tarball"}
    if size > MAX_TARBALL_BYTES:
        return {"error": f"tarball_too_large_{MAX_TARBALL_BYTES}", "code": "tarball_too_large"}

    sha256_hex = hashlib.sha256(tarball_bytes).hexdigest()

    # ForkVersion has no DB unique constraint on (fork_id, semver), so the
    # REST endpoint's `except IntegrityError` never fires. Pre-check explicitly
    # for an idempotent, deterministic version_exists response.
    dupe = db.query(ForkVersion).filter(ForkVersion.fork_id == fork.id, ForkVersion.semver == semver).first()
    if dupe is not None:
        return {"error": f"version_exists: {semver}", "code": "version_exists"}

    forks_root = _forks_dir().resolve()
    dest_dir = (forks_root / str(fork.user_id) / fork.slug).resolve()
    if not str(dest_dir).startswith(str(forks_root) + os.sep):
        return {"error": "path_traversal_detected", "code": "path_traversal_detected"}
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{semver}.tar.gz"
    dest_path.write_bytes(tarball_bytes)
    try:
        dest_path.chmod(0o640)
    except OSError:
        pass

    version = ForkVersion(
        id=uuid4(),
        fork_id=fork.id,
        semver=semver,
        tarball_path=str(dest_path),
        tarball_size_bytes=size,
        checksum_sha256=sha256_hex,
        changelog=changelog,
    )
    db.add(version)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return {"error": f"version_exists: {semver}", "code": "version_exists"}

    fork.latest_version_id = version.id
    db.commit()
    db.refresh(version)

    return {
        "status": "versioned",
        "fork_id": str(fork.id),
        "fork_slug": fork.slug,
        "version_id": str(version.id),
        "semver": version.semver,
        "tarball_size_bytes": int(version.tarball_size_bytes),
        "checksum_sha256": version.checksum_sha256,
        "changelog": version.changelog,
        "message": (
            "Version uploaded. Deploy into a cookbook via recipes_cookbook_attach "
            "to make it installable like a catalog skill."
        ),
    }


def recipes_cookbook_attach(
    db: Session,
    *,
    fork_id: str,
    target_cookbook_id: str,
    slug: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Deploy a tailored fork's latest version into a cookbook (the bridge).

    Promotes the fork's latest ForkVersion into a real catalog Skill (private,
    is_public=False) linked into the target cookbook via write_cookbook_skill,
    AND mints a SkillVersion from the same tarball so recipes_cookbook_install
    can resolve an installable version. The promoted skill installs with the
    canonical recipes-skill-install salt automatically (it's a real Skill row).

    Args:
        fork_id: the caller's fork to deploy.
        target_cookbook_id: cookbook to attach the promoted skill to (caller must own).
        slug: optional override for the promoted skill slug (defaults to the
            fork slug). Must match ^[a-z0-9][a-z0-9_-]{0,63}$.

    Tier: Pro or above. Returns the promoted skill + version, or an error.
    """
    gate = _require_pro_user(ctx)
    if gate is not None:
        return gate
    assert ctx is not None

    fork = _resolve_owned_fork(db, ctx, fork_id)
    if fork is None:
        return {"error": "fork_not_found", "code": "fork_not_found"}

    if not fork.latest_version_id:
        return {
            "error": "no_versions",
            "code": "no_versions",
            "message": "Fork has no uploaded version. Call recipes_tailor_version first.",
        }
    fork_version = db.query(ForkVersion).filter(ForkVersion.id == fork.latest_version_id).first()
    if fork_version is None:
        return {"error": "no_versions", "code": "no_versions"}

    # Ownership of the target bundle (no-oracle: forbidden → cookbook_not_found).  # compat-alias
    try:
        from uuid import UUID

        cb_id = UUID(str(target_cookbook_id))
    except (ValueError, TypeError):
        return {"error": "cookbook_not_found", "code": "cookbook_not_found"}
    cb = db.query(Cookbook).filter(Cookbook.id == cb_id).first()
    if cb is None or not authz.can_write_cookbook(ctx, cb):
        return {"error": "cookbook_not_found", "code": "cookbook_not_found"}

    # Read the fork tarball from disk.
    tar_path = Path(fork_version.tarball_path)
    if not tar_path.is_file():
        return {"error": "tarball_missing_on_disk", "code": "tarball_missing_on_disk"}
    tarball_bytes = tar_path.read_bytes()

    # Extract the SKILL.md body to seed the catalog Skill's readme.
    skill_md = _extract_skill_md(tarball_bytes)
    if not skill_md or not skill_md.strip():
        return {
            "error": "no_skill_md_in_tarball",
            "code": "no_skill_md_in_tarball",
            "message": "The fork's latest version tarball has no readable SKILL.md.",
        }

    promoted_slug = (slug or fork.slug).strip().lower()
    if not re.match(r"^[a-z0-9][a-z0-9_-]{0,63}$", promoted_slug):
        return {"error": "invalid_slug", "code": "invalid_slug", "slug": promoted_slug}

    # 1) Promote into a real Skill + CookbookSkill (private catalog unit).
    try:
        classification = classify_skill(skill_md)
        cs, status = write_cookbook_skill(
            slug=promoted_slug,
            content=skill_md,
            target_cookbook_id=cb.id,
            visibility="private",
            db=db,
            classifier=classification,
            owner_user_id=ctx.user_id,
            tier=ctx.tier or "pro",
            is_public=False,
            ctx=ctx,
        )
    except ValidationError as exc:
        return {"error": str(exc), "code": "invalid_input"}

    skill = db.query(Skill).filter(Skill.slug == promoted_slug).first()
    if skill is None:  # defensive — write_cookbook_skill just upserted it
        return {"error": "promote_failed", "code": "promote_failed"}

    # 2) Mint a SkillVersion from the SAME tarball so cookbook_install resolves
    #    an installable version. Reuse the fork version's semver + checksum.
    semver = fork_version.semver
    existing_v = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill.id, SkillVersion.semver == semver)
        .first()
    )
    if existing_v is None:
        try:
            stored_path = _store_skill_tarball(promoted_slug, semver, tarball_bytes)
        except ValueError as exc:
            return {"error": str(exc), "code": "store_failed"}
        version_row = SkillVersion(
            id=uuid4(),
            skill_id=skill.id,
            semver=semver,
            tarball_path=stored_path,
            tarball_size_bytes=len(tarball_bytes),
            checksum_sha256=hashlib.sha256(tarball_bytes).hexdigest(),
            changelog=fork_version.changelog,
        )
        db.add(version_row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            version_row = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill.id, SkillVersion.semver == semver)
                .first()
            )
        else:
            db.refresh(version_row)
        version_id = version_row.id if version_row else None
    else:
        version_id = existing_v.id

    return {
        "status": status,  # "created" | "updated" from write_cookbook_skill
        "fork_id": str(fork.id),
        "skill_slug": promoted_slug,
        "skill_id": str(skill.id),
        "cookbook_id": str(cb.id),
        "version": semver,
        "version_id": str(version_id) if version_id else None,
        "is_public": False,
        "message": (
            f"Fork deployed into cookbook as private skill {promoted_slug!r}. "
            "Install it (or share the cookbook) via recipes_cookbook_install."
        ),
    }
