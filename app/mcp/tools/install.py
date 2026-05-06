"""recipes_install — mirror of ``GET /api/skills/install`` for MCP callers.

Returns a signed tarball URL, sha256 checksum and manifest. The HTTP handler
also writes an InstallEvent row; we replicate that here so analytics stay
consistent across transports.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.config import settings
from app.models import InstallEvent, Skill
from app.routes import _build_manifest


def recipes_install(
    db: Session,
    slug: str,
    api_key_id: Any | None = None,
) -> dict[str, Any]:
    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if not skill:
        return {"error": "not_found", "slug": slug}
    if not skill.versions:
        return {"error": "no_versions", "slug": slug}

    latest = skill.versions[0]
    serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET)
    token = serializer.dumps(
        {"slug": slug, "version_id": str(latest.id), "mode": "files"}
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
            skill_slug=slug,
            api_key_id=api_key_id,
            version_semver=latest.semver,
            client_ip=None,
        )
    )
    db.commit()

    return {
        "slug": slug,
        "version": latest.semver,
        "tarball_url": tarball_url,
        "checksum_sha256": latest.checksum_sha256,
        "size_bytes": latest.tarball_size_bytes,
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat(),
        "manifest": _build_manifest(latest, skill),
    }
