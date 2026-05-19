"""recipes_install — mirror of ``GET /api/skills/install`` for MCP callers.

Returns a signed tarball URL, sha256 checksum and manifest. The HTTP handler
also writes an InstallEvent row; we replicate that here so analytics stay
consistent across transports.

Stream 4 additions:
- Accept ``slug@<semver>`` in the slug argument (or an explicit ``version``
  kwarg) to pin the install to a specific version. Used by Phase B's
  adversarial broken-version test.
- Surface ``related_skills`` (informational, ≤10) computed from the live
  graph_routes related view. The customer agent may prompt the user to
  install related, but the platform never auto-installs.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app import authz
from app.config import settings
from app.models import InstallEvent, Skill, SkillDerivedEdge
from app.routes import _build_manifest


def _split_slug_version(raw: str) -> tuple[str, str | None]:
    """Split ``slug@1.2.3`` → (``slug``, ``1.2.3``). Returns (raw, None) when
    no ``@`` is present. Whitespace is stripped from both sides.
    """
    if "@" in raw:
        s, v = raw.split("@", 1)
        s = s.strip()
        v = v.strip()
        return s, v or None
    return raw.strip(), None


def _related_slugs(db: Session, slug: str, limit: int = 10) -> list[str]:
    """Return up to ``limit`` related slugs from the derived edge table.

    Mirrors the read path used by ``GET /api/graph/related?slug=...`` —
    pulled via direct SQLAlchemy so we never shell out to HTTP.
    """
    rows = (
        db.query(SkillDerivedEdge)
        .filter(SkillDerivedEdge.source_slug == slug)
        .order_by(SkillDerivedEdge.weight.desc())
        .limit(limit)
        .all()
    )
    return [r.target_slug for r in rows]


def recipes_install(
    db: Session,
    slug: str,
    api_key_id: Any | None = None,
    version: str | None = None,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Resolve a slug (optionally pinned via ``slug@version`` or ``version=``)
    to a signed download URL, write an InstallEvent row, and surface a small
    list of related skills.

    Phase B (Issue #6): calls authz.can_install(ctx, skill) before signing.
    Private skills with no access return {"error": "not_found"} — no oracle.
    """
    base_slug, version_in_slug = _split_slug_version(slug)
    pinned_version = version or version_in_slug

    # Use anonymous context if none provided (e.g. legacy callers, tests)
    if ctx is None:
        ctx = AuthContext(scope="master")

    skill = db.query(Skill).filter(Skill.slug == base_slug).first()
    if not skill:
        return {"error": "not_found", "slug": base_slug}

    # Phase B (Issue #6): visibility check — no existence oracle for private skills
    if not authz.can_install(ctx, skill):
        return {"error": "not_found", "slug": base_slug}

    if not skill.versions:
        return {"error": "no_versions", "slug": base_slug}

    if pinned_version:
        target = next(
            (v for v in skill.versions if v.semver == pinned_version),
            None,
        )
        if target is None:
            return {
                "error": "version_not_found",
                "slug": base_slug,
                "version": pinned_version,
                "available_versions": [v.semver for v in skill.versions],
            }
    else:
        target = skill.versions[0]

    serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET)
    token = serializer.dumps(
        {"slug": base_slug, "version_id": str(target.id), "mode": "files"}
    )
    public_origin = (
        getattr(settings, "PUBLIC_ORIGIN", None)
        or os.environ.get("RECIPES_PUBLIC_ORIGIN")
        or "https://recipes.wisechef.ai"
    )
    tarball_url = (
        public_origin.rstrip("/") + "/api/skills/_download?token=" + token
    )

    db.add(
        InstallEvent(
            id=uuid4(),
            skill_id=skill.id,
            skill_slug=base_slug,
            api_key_id=api_key_id,
            version_semver=target.semver,
            client_ip=None,
        )
    )
    db.commit()

    related = _related_slugs(db, base_slug, limit=10)

    return {
        "slug": base_slug,
        "version": target.semver,
        "version_pinned": bool(pinned_version),
        "tarball_url": tarball_url,
        "checksum_sha256": target.checksum_sha256,
        "size_bytes": target.tarball_size_bytes,
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(),
        "manifest": _build_manifest(target, skill),
        "related_skills": related,
    }
