"""Private skill-publishing endpoint — WIS-SP2.

POST /api/skills/_publish
  - Multipart form: skill_toml, tarball, signature, signing_pubkey, [is_public=false], [changelog]
  - Requires x-api-key header validated by APIKeyMiddleware (sets request.state.api_key_user_id)
  - Creator-only: api_key.user_id must match skill.creator.user_id (or static master key = admin)
  - ed25519 signature verified over sha256(tarball bytes)
  - Tarball stored at RECIPES_SKILLS_DIR/{slug}/{semver}.tar.gz (mode 0640)
  - Creates skill_versions row; converts IntegrityError → 409 version_exists
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tomllib
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_der_public_key
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app._creator_helpers import _resolve_or_create_creator
from app.auth_ctx import AuthContext
from app.config import settings
from app.database import get_db
from app.models import CookbookSkill, Creator, Skill, SkillVersion
from app.security_scan import scan_tarball
from app.sync_fanout import emit_cookbook_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["publisher"])

# 10 MB hard limit on tarball
MAX_TARBALL_BYTES = 10 * 1024 * 1024

# Required keys in [skill] section of skill.toml
REQUIRED_TOML_FIELDS = {"name", "version", "description", "license", "entrypoint"}

# Env-configurable storage root
SKILLS_DIR_DEFAULT = "/var/lib/recipes-skills"

# Slug and version validation patterns (path traversal prevention)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(-[a-zA-Z0-9.-]+)?$")


def _skills_dir() -> Path:
    """Return the configured skills storage directory.

    Respects WR_RECIPES_SKILLS_DIR (settings prefix) then RECIPES_SKILLS_DIR fallback.
    Check raw env first so test patches (patch.dict os.environ) take effect.
    """
    # F-API-06: check raw env var first (test-patch compatible), then settings (WR_ prefix), then default
    return Path(
        os.environ.get("RECIPES_SKILLS_DIR")
        or getattr(settings, "RECIPES_SKILLS_DIR", None)
        or SKILLS_DIR_DEFAULT
    )


class PublishResponse(BaseModel):
    skill_id: str
    version: str
    tarball_path: str
    sha256: str
    warnings: list[dict] = []


def _parse_and_validate_toml(toml_bytes: bytes) -> dict:
    """Parse skill.toml bytes and return the [skill] section dict.

    Raises HTTPException 422 if required fields are missing or TOML is invalid.
    """
    try:
        data = tomllib.loads(toml_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"skill.toml parse error: {exc}")

    skill_section = data.get("skill")
    if not isinstance(skill_section, dict):
        raise HTTPException(
            status_code=422,
            detail="skill.toml must contain a [skill] section",
        )

    missing = REQUIRED_TOML_FIELDS - set(skill_section.keys())
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"skill.toml missing required fields: {sorted(missing)}",
        )

    return skill_section


def _verify_ed25519(pubkey_bytes: bytes, signature_bytes: bytes, data: bytes) -> None:
    """Verify an ed25519 signature.  Raises HTTPException 400 on failure.

    pubkey_bytes — raw 32-byte ed25519 public key (DER-encoded public keys also accepted).
    signature_bytes — raw 64-byte ed25519 signature.
    data — the message that was signed.
    """
    try:
        if len(pubkey_bytes) == 32:
            # Raw key — construct directly
            pub = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
        else:
            # Assume DER SubjectPublicKeyInfo
            pub = load_der_public_key(pubkey_bytes)
        pub.verify(signature_bytes, data)
    # Rationale: cryptographic verify can raise many exception types; catch all as invalid_signature
    except (InvalidSignature, ValueError, TypeError, Exception) as exc:  # noqa: BLE001
        logger.debug("ed25519 verify failed: %s", exc)
        raise HTTPException(status_code=400, detail="invalid_signature")


def _require_api_key_user(request: Request) -> UUID | None:
    """Return the user_id associated with the validated API key, or None for the master key."""
    # APIKeyMiddleware sets api_key_user_id; None means static master key (admin)
    return getattr(request.state, "api_key_user_id", "MISSING")


def _extract_skill_md_from_tarball(tarball_bytes: bytes) -> str | None:
    """Extract the SKILL.md text from a publish tarball.

    fix_2005: the publish path historically only persisted SKILL.md content
    via a separate Recipify call. When a skill is published directly via
    /api/skills/_publish (no prior Recipify), skills.readme stays NULL forever
    and the catalog renders the Day-1 placeholder on the skill's portal page.

    Looks for SKILL.md at three plausible locations (root, single-dir nesting,
    and `recipes/<slug>/SKILL.md` for safety). Returns the decoded text, or
    None if not found / unreadable. Bounded by 256 KB to avoid memory blowup
    on malicious tarballs.
    """
    import io
    import tarfile

    MAX_BYTES = 256 * 1024
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as t:
            candidates: list[tarfile.TarInfo] = []
            for m in t.getmembers():
                if not m.isfile():
                    continue
                name = m.name.lstrip("./")
                # Match SKILL.md at root, one level deep, or recipes/<slug>/SKILL.md
                parts = name.split("/")
                if parts[-1] != "SKILL.md":
                    continue
                if len(parts) <= 3:
                    candidates.append(m)
            if not candidates:
                return None
            # Prefer the shortest path (root SKILL.md wins over nested copies)
            candidates.sort(key=lambda m: len(m.name))
            chosen = candidates[0]
            if chosen.size > MAX_BYTES:
                return None
            f = t.extractfile(chosen)
            if f is None:
                return None
            raw = f.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                return None
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
    # Rationale: any tarball parsing failure (corrupted, wrong format, etc.)
    # must not block the publish — readme sync is best-effort.
    except Exception as exc:  # noqa: BLE001
        logger.warning("readme extraction from tarball failed: %s", exc)
        return None


def _store_tarball(slug: str, semver: str, tarball_bytes: bytes) -> str:
    """Write tarball to disk at {SKILLS_DIR}/{slug}/{semver}.tar.gz.

    Returns the path string. File mode is 0640.
    Includes defense-in-depth path traversal check.
    """
    skills_root = _skills_dir().resolve()
    dest_dir = (skills_root / slug).resolve()
    # Defense-in-depth: ensure resolved path stays inside skills root
    if not str(dest_dir).startswith(str(skills_root) + "/"):
        raise HTTPException(422, detail="path traversal detected")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{semver}.tar.gz"
    dest_path.write_bytes(tarball_bytes)
    dest_path.chmod(0o640)
    return str(dest_path)


@router.post("/_publish", response_model=PublishResponse, status_code=201, tags=["publisher"])
async def publish_skill(
    request: Request,
    skill_toml: UploadFile = File(..., description="skill.toml manifest file"),
    tarball: UploadFile = File(..., description="Skill tarball (.tar.gz)"),
    signature: UploadFile = File(..., description="ed25519 signature over sha256(tarball)"),
    signing_pubkey: UploadFile = File(..., description="ed25519 public key (raw 32 bytes or DER)"),
    is_public: bool = Form(False, description="Publish as public skill (default: private)"),
    changelog: str | None = Form(None, description="Optional changelog for this version"),
    db: Session = Depends(get_db),
):
    """Publish a new skill version (private by default).

    Auth: x-api-key must belong to the skill's creator (or be the admin master key).
    Signature: ed25519 signature must verify against sha256(tarball bytes).
    Storage: tarball saved to RECIPES_SKILLS_DIR/{slug}/{semver}.tar.gz
    DB: creates skill_versions row; returns 409 if (skill_id, semver) already exists.
    """
    # ── 1. Read and size-check tarball ──────────────────────────────────
    tarball_bytes = await tarball.read()
    if len(tarball_bytes) > MAX_TARBALL_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Tarball exceeds maximum size of {MAX_TARBALL_BYTES} bytes (10 MB)",
        )

    # ── 2. Parse and validate skill.toml ────────────────────────────────
    toml_bytes = await skill_toml.read()
    if not toml_bytes:
        raise HTTPException(status_code=422, detail="skill_toml is required")

    skill_section = _parse_and_validate_toml(toml_bytes)
    skill_name = skill_section["name"]
    semver = skill_section["version"]
    skill_description = skill_section.get("description", "")

    # ── 3. Read signature and public key ────────────────────────────────
    sig_bytes = await signature.read()
    pubkey_bytes = await signing_pubkey.read()

    # ── 4. ed25519 signature verification ───────────────────────────────
    # Sign message = sha256(tarball bytes)
    tarball_sha256 = hashlib.sha256(tarball_bytes).digest()
    _verify_ed25519(pubkey_bytes, sig_bytes, tarball_sha256)

    # ── 5. §7.2 Security scan ────────────────────────────────────────────
    # Run AFTER signature verification, BEFORE writing anything to disk.
    findings = scan_tarball(tarball_bytes, skill_section)
    high_findings = [f for f in findings if f.severity == "high"]
    if high_findings:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "security_scan_failed",
                "findings": [
                    {
                        "class": f.pattern_class,
                        "file": f.file_path,
                        "line": f.line_no,
                        "snippet": f.snippet[:200],
                        "why": f.rationale,
                    }
                    for f in high_findings
                ],
            },
        )
    # medium/low findings are returned in the success response as warnings
    warnings = [
        {
            "class": f.pattern_class,
            "file": f.file_path,
            "line": f.line_no,
            "snippet": f.snippet[:200],
            "why": f.rationale,
        }
        for f in findings
        if f.severity in ("medium", "low")
    ]

    # ── 5a. A.7 discipline linter (BLOCKING pre-check) ──────────────────
    # Enforces the skill-discipline anti-patterns (no user names, no curl|bash,
    # no hardcoded /home paths, etc.) so that published skills are portable
    # across users, hosts, and agent personalities. Runs early; rejects the
    # publish entirely with HTTP 422 if any violation is found.
    try:
        import sys as _sys

        _repo_root = str(Path(__file__).resolve().parent.parent)
        if _repo_root not in _sys.path:
            _sys.path.insert(0, _repo_root)
        from scripts.skill_discipline_linter import lint_tarball_bytes as _discipline_lint

        discipline_result = _discipline_lint(tarball_bytes)
        if not discipline_result["ok"]:
            raise HTTPException(
                status_code=422,
                detail={
                    "ok": False,
                    "error": "discipline_lint_failed",
                    "violations": discipline_result["violations"],
                },
            )
    except ImportError:
        logger.warning("skill_discipline_linter not importable; skipping A.7 gate")

    # ── 5b. Quality gate (leak audit + generalization) ──────────────────
    # Defense-in-depth: scan_tarball() above catches malicious patterns;
    # this catches internal-info leakage (IPs, UUIDs, hostnames, hardcoded paths).
    # Block-level findings here are categories like internal_uuid, public_ipv4,
    # ssh_user_combo, discord_mention, real credentials, hetzner_internal.
    try:
        from app.skill_quality_gate import scan_tarball_bytes as _gate_scan

        gate_findings = _gate_scan(tarball_bytes)
        gate_blocks = [f for f in gate_findings if f["severity"] == "block"]
        if gate_blocks:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "quality_gate_failed",
                    "stage": "leak_or_generalization",
                    "findings": gate_blocks[:25],  # cap response size
                    "total_blocks": len(gate_blocks),
                    "remediation": (
                        "Found internal infra references (IPs, UUIDs, internal hostnames, "
                        "credentials) in the published skill. See skill_quality_gate "
                        "categories. Generalize to env vars or remove."
                    ),
                },
            )
        # Append non-blocking gate warnings to the response warnings list
        warnings.extend([{**f, "source": "quality_gate"} for f in gate_findings if f["severity"] == "warn"])
    except ImportError:
        # Module not installed yet (rolling deploy) — fail open with a log line
        logger.warning("skill_quality_gate not importable; skipping gate scan")

    # ── 6. Resolve skill record ──────────────────────────────────────────
    # We use the name field from skill.toml to look up by slug convention.
    # Skills are keyed by their slug (derived from name). If the skill doesn't
    # exist, we create it (private) so creators can publish new skills.
    slug = skill_section.get("slug", skill_name.lower().replace(" ", "-"))

    # F-API-02: validate slug and semver before any filesystem operations
    if not SLUG_RE.match(slug):
        raise HTTPException(422, detail=f"Invalid slug: {slug!r}")
    if not SEMVER_RE.match(semver):
        raise HTTPException(422, detail=f"Invalid version: {semver!r}")

    skill_obj = db.query(Skill).filter(Skill.slug == slug).first()
    if not skill_obj:
        # F-API-03: auto-create skill and set creator_id from the authenticated user
        # Master key publishes (no user) leave creator_id = NULL (admin-owned)
        api_key_user_id_for_create = getattr(request.state, "api_key_user_id", "MISSING")
        is_master_for_create = api_key_user_id_for_create is None

        # Look up or create Creator row via shared helper
        creator_for_new_skill = None
        if not is_master_for_create and api_key_user_id_for_create != "MISSING":
            _create_ctx = AuthContext(scope="user", user_id=api_key_user_id_for_create)
            creator_for_new_skill = _resolve_or_create_creator(_create_ctx, db)

        skill_obj = Skill(
            id=uuid4(),
            slug=slug,
            title=skill_name,
            description=skill_description,
            license=skill_section.get("license"),
            tier=(skill_section.get("tier") or "").strip() or None,
            readme=_extract_skill_md_from_tarball(tarball_bytes),
            is_public=is_public,
            creator_id=creator_for_new_skill.id if creator_for_new_skill else None,
        )
        db.add(skill_obj)
        db.flush()  # get the id without committing

    # ── 6. Auth: caller must be the skill's creator or admin ─────────────
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    is_master_key = api_key_user_id is None  # None = static master key (admin)

    if not is_master_key:
        if api_key_user_id == "MISSING":
            raise HTTPException(status_code=401, detail="Authentication required")

        # Resolve the creator for this skill
        creator = None
        if skill_obj.creator_id:
            creator = db.query(Creator).filter(Creator.id == skill_obj.creator_id).first()

        if creator is None or creator.user_id != api_key_user_id:
            raise HTTPException(
                status_code=403,
                detail="You are not authorised to publish this skill",
            )

    # ── 7. Update mutable metadata on the skill row from this publish ────
    # F-API-04 (RCP-PUB-2026-05-18): mutable manifest fields (description,
    # license, tier, title) live on the SkillVersion's tarball, but the public
    # /api/skills/<slug> endpoint reads them off the parent Skill row. Without
    # this sync, the row keeps stale values from whatever code path created it
    # (recipify, manual seed, archived row from a previous lifecycle). Each
    # publish re-asserts the new tarball's frontmatter as the authoritative
    # state for the row.
    if is_public and not skill_obj.is_public:
        skill_obj.is_public = True

    # Description, license, title: re-sync from skill_section every publish.
    # Only overwrite when the new value is non-empty AND differs — preserves
    # editorial overrides from quality_1705 backfills until those are
    # explicitly replaced by a published tarball with a real description.
    new_desc = (skill_description or "").strip()
    if new_desc and new_desc != (skill_obj.description or "").strip():
        skill_obj.description = new_desc
    new_license = (skill_section.get("license") or "").strip()
    if new_license and new_license != (skill_obj.license or "").strip():
        skill_obj.license = new_license
    new_title = (skill_name or "").strip()
    if new_title and new_title != (skill_obj.title or "").strip():
        skill_obj.title = new_title
    new_tier = (skill_section.get("tier") or "").strip()
    if new_tier and new_tier != (skill_obj.tier or "").strip():
        skill_obj.tier = new_tier

    # fix_2005: readme sync from tarball SKILL.md. The catalog detail endpoint
    # reads skills.readme; without this sync, a row whose readme was never set
    # (Recipify-skipped path, direct master-key publish, etc.) stays NULL forever
    # and the portal renders the Day-1 placeholder. Best-effort: a tarball
    # without SKILL.md or that fails to decode is logged but does NOT block
    # the publish (versioned tarball is the canonical artifact).
    skill_md_text = _extract_skill_md_from_tarball(tarball_bytes)
    if skill_md_text:
        current_readme = (skill_obj.readme or "").strip()
        if skill_md_text.strip() != current_readme:
            skill_obj.readme = skill_md_text
            logger.info(
                "publish: synced readme for %s from tarball SKILL.md (%d chars)",
                slug,
                len(skill_md_text),
            )

    # Un-archive a previously-archived skill row when a fresh public version
    # lands. Without this, the portal build silently skips the slug (it
    # filters on is_archived=false) and the publish CLI's step-13 verification
    # raises `portal dist missing skills/<slug>/index.html`. Surfaced as
    # "Published-not-portalized" drift by recipes_publish_watchdog.py.
    if is_public and getattr(skill_obj, "is_archived", False):
        skill_obj.is_archived = False
        if hasattr(skill_obj, "archived_at"):
            skill_obj.archived_at = None
        logger.info(
            "publish: unarchived skill %s on publish of v%s (was hidden from catalog)",
            slug,
            semver,
        )

    # ── 8. Compute final sha256 (hex) ────────────────────────────────────
    sha256_hex = hashlib.sha256(tarball_bytes).hexdigest()

    # ── 9. Store tarball on disk ─────────────────────────────────────────
    try:
        tarball_path = _store_tarball(slug, semver, tarball_bytes)
    except OSError as exc:
        logger.error("Failed to store tarball for %s@%s: %s", slug, semver, exc)
        raise HTTPException(status_code=500, detail=f"Storage error: {exc}")

    # ── 10. Create skill_versions row ────────────────────────────────────
    version_row = SkillVersion(
        id=uuid4(),
        skill_id=skill_obj.id,
        semver=semver,
        tarball_path=tarball_path,
        tarball_size_bytes=len(tarball_bytes),
        checksum_sha256=sha256_hex,
        changelog=changelog,
        skill_toml=toml_bytes.decode("utf-8", errors="replace"),
    )
    db.add(version_row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        detail_str = str(exc).lower()
        if "uq_skill_version" in detail_str or "unique constraint" in detail_str:
            raise HTTPException(
                status_code=409,
                detail=f"version_exists: {slug}@{semver} already published",
            )
        raise

    db.refresh(skill_obj)
    db.refresh(version_row)

    # ── 10a. BM25 reindex (Phase 4) ──────────────────────────────────────
    # Embeddings deferred to v7.2; BM25-only per Adam directive 2026-05-07.
    # Synchronous — to_tsvector is <10ms in postgres.
    try:
        from app.search_index import reindex_bm25

        reindex_bm25(skill_obj.slug, db)
    # Rationale: BM25 reindex is non-critical at publish time; failure → log and continue
    except Exception:  # noqa: BLE001
        logger.exception("BM25 reindex failed for %s (non-fatal)", skill_obj.slug)

    # ── 11. Live-sync fan-out (Phase D) ─────────────────────────────────
    # Notify every cookbook that has this skill (and isn't disabled). On
    # Postgres this goes via pg_notify so all processes receive it; on
    # SQLite tests it publishes directly to the in-process subscribers.
    try:
        cookbook_ids = [
            str(cs.bundle_id)
            for cs in db.query(CookbookSkill)
            .filter(
                CookbookSkill.skill_id == skill_obj.id,
                CookbookSkill.source != "disabled",
            )
            .all()
        ]
        if cookbook_ids:
            await emit_cookbook_event(
                db,
                cookbook_ids,
                {
                    "slug": skill_obj.slug,
                    "version": semver,
                    "action": "version_published",
                    "skill_id": str(skill_obj.id),
                },
            )
            db.commit()
    # Rationale: fanout is non-critical at publish time; any error → log and return response
    except Exception:  # noqa: BLE001
        logger.exception("phase-D fan-out failed for %s@%s (non-fatal)", slug, semver)

    return PublishResponse(
        skill_id=str(skill_obj.id),
        version=semver,
        tarball_path=tarball_path,
        sha256=sha256_hex,
        warnings=warnings,
    )


# ── Phase 4: Skill Archive ──────────────────────────────────────────────


class ArchiveResponse(BaseModel):
    slug: str
    archived: bool


@router.post("/{slug}/_archive", response_model=ArchiveResponse, tags=["publisher"])
async def archive_skill(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Soft-archive a skill — sets is_archived=True and NULLs search_vector.

    Auth: master key only (admin). Archived skills are hidden from /api/recall
    but remain in the DB for audit/recovery.
    """
    from app.search_index import reindex_bm25

    # Master-key only: api_key_user_id must be None
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    if api_key_user_id is not None:
        raise HTTPException(status_code=403, detail="Admin only")

    if not SLUG_RE.match(slug):
        raise HTTPException(422, detail=f"Invalid slug: {slug!r}")

    skill_obj = db.query(Skill).filter(Skill.slug == slug).first()
    if not skill_obj:
        raise HTTPException(status_code=404, detail="Skill not found")

    skill_obj.is_archived = True
    db.flush()

    # NULL the search vector so the skill drops from recall results
    reindex_bm25(slug, db, archive=True)

    return ArchiveResponse(slug=slug, archived=True)
