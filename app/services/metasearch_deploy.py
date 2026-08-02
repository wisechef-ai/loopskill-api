"""Deploy-time resolve-and-pin for external skills (metasearch_0710 P3).

THE north-star mechanism: a metasearch result (skills.sh / github tap) deploys to
a fleet in one motion. P1 proved the card resolves; P2 rendered it deployable;
P3 pins a SERVER-SIDE ARTIFACT so 30k agents reconcile against OUR bytes — never
re-resolving upstream (§7.5 invariant, the scale ship-blocker).

Council P3 review (2026-07-11) corrected the first cut: pinning only a SHA is not
enough — reconcile serves a signed tarball from ``/api/skills/_download`` keyed by
``SkillVersion.checksum_sha256`` + ``tarball_path``. An external skill had no
``SkillVersion``, so an agent could not pull the pinned bytes and would re-resolve
upstream (§7.5 violation). Fix: at deploy we resolve the SKILL.md ONCE, pack it
into an immutable ``{sha}.tar.gz`` under ``RECIPES_SKILLS_DIR``, and create a
``SkillVersion`` row (semver = the sha, checksum_sha256 = the sha) — reusing the
ENTIRE existing curated serve/reconcile machinery with zero new reconcile code.
The artifact is immutable + content-addressed, so a re-deploy to fleet B never
mutates fleet A's pinned bytes (the SkillVersion is keyed by content sha).

FK coupling (planning-council C3): ``materialize_external_skill`` mints a real
private ``Skill`` row (``skills.id``), satisfying ``bundle_skills.skill_id``.

decision #6 (ClawHub / condition 2b): ClawHub is preview-only — its
``route_install`` is DEEP_LINK so ``resolve_external_install`` returns no content
→ ``pin_external_for_deploy`` fails closed. Only skills.sh + github taps
(FETCH_ORIGIN, redistributable) pin. Enforced by the resolver, RED-proofed.
"""

from __future__ import annotations

import hashlib
import io
import logging
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.models import Skill

logger = logging.getLogger(__name__)


@dataclass
class PinResult:
    """Outcome of a deploy-time resolve-and-pin. ``pinned`` False = fail-closed
    (not fleet-deployable — ClawHub deep-link, origin outage). Never raises."""

    pinned: bool
    skill_id: str | None = None
    skill_uuid: object = None  # raw UUID for DB (skill_id is the JSON str form)
    slug: str | None = None
    pinned_sha: str | None = None
    pinned_semver: str | None = None  # the SkillVersion.semver reconcile selects
    pinned_raw_url: str | None = None
    scan_status: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "pinned": self.pinned,
            "skill_id": self.skill_id,
            "slug": self.slug,
            "pinned_sha": self.pinned_sha,
            "pinned_semver": self.pinned_semver,
            "pinned_raw_url": self.pinned_raw_url,
            "scan_status": self.scan_status,
            "reason": self.reason,
        }


def content_sha(content: str) -> str:
    """Bare sha256 hex of the SKILL.md bytes — the content address / pin identity."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _pack_tarball(dest_dir: Path, sha: str, content: str) -> tuple[str, int, str]:
    """Pack SKILL.md content into an immutable content-addressed ``{sha}.tar.gz``
    (a single ``SKILL.md`` member), matching the curated tarball shape the
    reconcile fetcher + skill_file_cache already read. Idempotent: the same sha
    always produces the same path; existing file is reused.

    Returns (path, size, tarball_sha256). Council R2: reconcile_fetch verifies the
    SHA of the TARBALL BYTES, not the source file — so we compute and return the
    tarball digest for SkillVersion.checksum_sha256. Deterministic packing (fixed
    mtime/mode/name) makes the tarball bytes — and thus their sha — reproducible.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    tar_path = dest_dir / f"{sha}.tar.gz"
    data = content.encode("utf-8")
    buf = io.BytesIO()
    # gzip mtime=0 (mtime arg) + tar mtime=0 → byte-reproducible archive.
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
        info = tarfile.TarInfo(name="SKILL.md")
        info.size = len(data)
        info.mtime = 0
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(data))
    tar_bytes = buf.getvalue()
    if not tar_path.is_file():
        tar_path.write_bytes(tar_bytes)
    tar_sha = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    return str(tar_path), tar_path.stat().st_size, tar_sha


def pin_external_for_deploy(db: "Session", source: str, slug: str) -> PinResult:
    """Resolve an external skill's SKILL.md ONCE, pack an immutable server-side
    artifact, and create a content-addressed ``SkillVersion`` — the deploy-time
    resolve (§7.5). Agents then reconcile against OUR tarball, never upstream.

    Fail-closed on any source that can't resolve to redistributable content
    (ClawHub deep-link, non-redistributable, origin outage, MCP-register).

    Exactly ONE upstream resolve (council MUST 2): the resolved content is packed
    directly; ``materialize_external_skill`` is only used for the pointer row and
    its re-resolve is avoided when the row already exists.
    """
    from app.config import settings
    from app.models import Skill, SkillVersion
    from app.services.bundle_external import external_slug, resolve_external_install

    # 1) THE single upstream call — resolve the live SKILL.md once.
    try:
        resolved = resolve_external_install(source, slug)
    except Exception:  # noqa: BLE001
        logger.warning("pin resolve failed for %s:%s", source, slug, exc_info=True)
        return PinResult(False, reason="resolve_error")

    if not resolved or not isinstance(resolved.get("content"), str) or not resolved["content"].strip():
        return PinResult(False, slug=slug, reason="not_pinnable_no_content")

    content = resolved["content"]
    sha = content_sha(content)
    # Council R2: SkillVersion.semver is String(32) and BundleSkill.pinned_version
    # is String(50) — the full "sha256:"+64hex (71 chars) overflows both on
    # PostgreSQL (SQLite doesn't enforce, which hid this). Use a compact
    # content-addressed semver that fits String(32): "x" + first 24 hex of the
    # content sha (25 chars). Collision-safe enough for pin identity (96 bits);
    # the FULL content sha stays in the descriptor and the TARBALL sha is the
    # integrity checksum reconcile verifies.
    semver = f"x{sha[:24]}"  # 25 chars ≤ 32; content-addressed version reconcile selects
    raw_url = resolved.get("raw_url")
    scan_status = resolved.get("scan_status")
    scannable = resolved.get("scannable")
    scan_findings = resolved.get("scan_findings")
    scan_warnings = resolved.get("scan_warnings")

    # 2) Get/create the FK-satisfying private Skill row WITHOUT a second upstream
    #    resolve (council MUST 2). If the pointer row doesn't exist we build it
    #    directly from the descriptor of THIS resolve — never calling
    #    materialize_external_skill (which would re-resolve + re-fetch to scan).
    cat_slug = external_slug(source, slug)
    skill = db.query(Skill).filter(Skill.slug == cat_slug).first()
    if skill is None:
        skill = Skill(
            id=uuid4(),
            slug=cat_slug,
            title=(resolved.get("slug") or slug),
            description=None,
            license=resolved.get("license"),
            is_public=False,  # ISOLATION WALL: never in the public catalog
            is_archived=False,
            tier="external",
            skill_variant="external",
            original_source_url=resolved.get("origin_url") or raw_url,
            external_resources={
                "federation_source": source,
                "external_slug": slug,
                "install_path": resolved.get("install_path"),
                "origin_url": resolved.get("origin_url") or raw_url,
                "redistributable": True,
                # Council R2 HIGH: preserve the FULL scan metadata materialize
                # would have set (scannable/findings/warnings), not just status,
                # so install_descriptor_for's trust card isn't degraded.
                "scan_status": scan_status,
                "scannable": scannable,
                "scan_findings": scan_findings,
                "scan_warnings": scan_warnings,
            },
        )
        db.add(skill)
        db.flush()

    # 3) Pack the immutable artifact + content-addressed SkillVersion so reconcile's
    #    existing _attach_tarball_urls serves it exactly like a curated skill.
    try:
        dest = Path(settings.RECIPES_SKILLS_DIR) / cat_slug.replace(":", "_")
        tar_path, size, tarball_sha = _pack_tarball(dest, sha, content)
    except Exception:  # noqa: BLE001
        logger.warning("pin tarball pack failed for %s:%s", source, slug, exc_info=True)
        return PinResult(False, slug=slug, reason="artifact_pack_failed")

    existing_ver = (
        db.query(SkillVersion)
        .filter(SkillVersion.skill_id == skill.id, SkillVersion.semver == semver)
        .first()
    )
    if existing_ver is not None:
        # Council R3: the 25-char semver is a TRUNCATED (96-bit) content selector.
        # If an existing version with this semver has a DIFFERENT full-content sha
        # (a prefix collision — accidental ~2^-96, or an adversarially-crafted
        # ~2^48 pair), reusing it would silently serve stale/other bytes. The full
        # content sha lives in the descriptor changelog; detect a mismatch and
        # fail closed rather than serve the wrong artifact.
        prior = existing_ver.changelog or ""
        if f"content_sha={sha}" not in prior:
            logger.error("pin semver collision for %s:%s (semver=%s) — failing closed", source, slug, semver)
            return PinResult(False, slug=slug, reason="pin_semver_collision")
    else:
        db.add(
            SkillVersion(
                id=uuid4(),
                skill_id=skill.id,
                semver=semver,
                tarball_path=tar_path,
                tarball_size_bytes=size,
                # Council R2 MUST1: reconcile_fetch verifies the SHA of the TARBALL
                # BYTES, not the source file. Store the tarball digest here.
                checksum_sha256=tarball_sha,
                # Full content sha embedded for R3 collision detection on reuse.
                changelog=f"deploy-time pin from {source}:{slug} content_sha={sha}",
            )
        )

    # 4) Record the pin on the pointer descriptor (audit; the AUTHORITATIVE pin
    #    for reconcile is BundleSkill.pinned_version = semver, per fleet row).
    descriptor: dict[str, Any] = dict(getattr(skill, "external_resources", None) or {})
    descriptor["pinned_sha"] = sha
    descriptor["pinned_semver"] = semver
    descriptor["pinned_at"] = datetime.now(timezone.utc).isoformat()
    descriptor["pinned_raw_url"] = raw_url
    descriptor["pinned_scan_status"] = scan_status
    skill.external_resources = descriptor
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(skill, "external_resources")
    except Exception:  # noqa: BLE001
        # Rationale: flag_modified() can only fail if the ORM attribute
        # instrumentation is missing/misconfigured for this mapped class —
        # a programmer error, not a data condition. If it fails, the JSON
        # mutation above is silently NOT persisted (db.add/flush below will
        # write the row but SQLAlchemy may not detect the in-place dict
        # change), so we must surface it instead of swallowing it silently.
        logger.warning(
            "flag_modified(external_resources) failed for skill %s — pin metadata may not persist to the DB",
            getattr(skill, "slug", "<unknown>"),
            exc_info=True,
        )
    db.add(skill)
    db.flush()

    return PinResult(
        pinned=True,
        skill_id=str(skill.id),
        skill_uuid=skill.id,
        slug=skill.slug,
        pinned_sha=sha,
        pinned_semver=semver,
        pinned_raw_url=raw_url,
        scan_status=scan_status,
        reason="pinned",
    )


def get_pin(skill: "Skill") -> str | None:
    """Read the deploy-time content semver pin from a materialized external skill,
    or None if never deployed. (Audit read; the authoritative per-fleet pin is
    BundleSkill.pinned_version.)"""
    descriptor = getattr(skill, "external_resources", None) or {}
    pin = descriptor.get("pinned_semver") if isinstance(descriptor, dict) else None
    return pin if isinstance(pin, str) and pin.startswith("x") and len(pin) == 25 else None
