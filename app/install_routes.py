"""Skills install routes — /api/skills/install + /api/skills/_download.

Extracted from app/routes.py (Phase E — secfix_1905).

Registers:
  GET /skills/install    — generate signed tarball download URL
  GET /skills/_download  — stream tarball by signed token

Also exports:
  download_tarball   — re-exportable for backwards compat (from app.routes import download_tarball)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app._skill_helpers import (
    _build_manifest,
    _count_today_installs,
    _resolve_caller_tier_for_install,
    _set_utm_ref_cookie,
)
from app.access_routes import TIER_INSTALL_LIMITS
from app.database import get_db
from app.models import InstallEvent, Skill, SkillVersion
from app.schemas import InstallResponse
from app.tier_labels import display_label
from app.utils.client_ip import _real_client_ip

router = APIRouter(tags=["skills"])


def _immutable_cache_headers(checksum_sha256: str | None) -> dict[str, str]:
    """Cache headers for an immutable, content-addressed skill tarball.

    evergreen_0206 Phase D (decision #18) — CDN-fronted delta pulls.

    A versioned tarball's bytes never change, so it can be cached forever at the
    edge. Cloudflare already fronts origin (config.py:173), so once these headers
    are present, repeat pulls of the same version are served from Cloudflare's
    edge and the weak origin disk is hit once-per-version globally. The
    checksum_sha256 IS the perfect cache validator (content address) → ETag.

    SAFETY: if we don't know the checksum, we cannot content-address the bytes,
    so we MUST NOT mark them immutable (a future mutation would serve stale
    bytes forever). Fall back to no-store — correctness over cache-hit.
    """
    if not checksum_sha256:
        return {"Cache-Control": "no-store"}
    return {
        "Cache-Control": "public, max-age=31536000, immutable",
        "ETag": f'"{checksum_sha256}"',
        "X-Checksum-SHA256": checksum_sha256,
    }


# WIS-903: Retired skill registry (shared with routes.py)
from pathlib import Path as _Path

_RETIREMENT_FILE = _Path(__file__).resolve().parent.parent / "retired-skills.txt"
_RETIRED_SKILLS: dict[str, str] = {}
if _RETIREMENT_FILE.exists():
    for _line in _RETIREMENT_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#"):
            _parts = _line.split(None, 1)
            if len(_parts) == 2:
                _RETIRED_SKILLS[_parts[0]] = _parts[1]


@router.get("/skills/install", response_model=InstallResponse, tags=["skills"])
def install_skill(
    request: Request,
    slug: str = Query(..., description="Skill slug; supports 'slug@semver' suffix"),
    mode: str = Query("files", pattern="^(files|full)$"),
    version: str | None = Query(
        None,
        description="Pin install to a specific semver. Overrides any '@version' suffix on slug.",
    ),
    ref: str | None = Query(None, description="UTM ref platform code (li, x, yt, ig, fb, agentpact)"),
    db: Session = Depends(get_db),
):
    """Return a signed URL for downloading the skill tarball.

    Public skills are installable by any valid api-key. Private skills are
    installable ONLY by the admin master key OR by the api-key whose user
    owns the skill (creator self-install — required for dogfooding).
    """
    # Stream 4: support 'slug@semver' inline pinning, or explicit ?version=
    if "@" in slug and version is None:
        slug, _v = slug.split("@", 1)
        version = (_v or "").strip() or None
    slug = slug.strip()
    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if not skill:
        # WIS-903: check retired skill registry
        _alt = _RETIRED_SKILLS.get(slug)
        if _alt:
            raise HTTPException(
                status_code=404,
                detail=f"This skill was retired 2026-05-07. See: {_alt} or contact support.",
            )
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")

    # polish_1805 item 1 — free-skill anonymous install path.
    # The middleware sets ``is_anonymous_free_install`` when the request
    # reached this route without an ``x-api-key`` header. The route enforces
    # the contract here (defence-in-depth + the actual gate — middleware just
    # waves the request through):
    #
    #   tier=free + public                  → 200 install
    #   tier=pro/pro_plus + anon           → 401 "Authentication required"
    #   private skill + anon                → 404 (no existence leak; mirrors
    #                                          the visibility-check default)
    #
    # The anonymous path uses ``api_key_user_id=None`` which is the SAME
    # sentinel value as the master/admin key. We MUST exclude anonymous
    # callers from the admin codepath in the visibility check below.
    is_anonymous_free_install = bool(getattr(request.state, "is_anonymous_free_install", False))
    if is_anonymous_free_install:
        if not skill.is_public:
            # Don't even tell anonymous callers that private skills exist.
            raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
        if (skill.tier or "").lower() != "free":
            raise HTTPException(
                status_code=401,
                detail="Authentication required to install this skill. Free skills install with no key.",
            )

    # repohygiene_2605/H.1 (Issue #290): cbt_token callers with
    # allow_public_catalog=True may install PUBLIC skills from the catalog.
    # cbt_token callers with allow_public_catalog=False are blocked here
    # (defence-in-depth: middleware blocks at the path level first, but the
    # route-level check ensures correctness even in test setups that bypass
    # the real middleware).
    auth_ctx = getattr(request.state, "auth_ctx", None)
    if auth_ctx is not None and getattr(auth_ctx, "scope", None) == "cbt_token":
        if not getattr(auth_ctx, "allow_public_catalog", False):
            raise HTTPException(
                status_code=403,
                detail="Share tokens can only access cookbook routes",
            )

    # Visibility check
    if not skill.is_public:
        api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
        # api_key_user_id is None for the master/admin key, UUID for a user key.
        # polish_1805 — anonymous free-install callers ALSO have api_key_user_id=None,
        # so we must check the is_anonymous_free_install flag explicitly before
        # treating None as admin.
        is_admin = api_key_user_id is None and not is_anonymous_free_install
        is_owner = (
            skill.creator
            and api_key_user_id is not None
            and api_key_user_id != "MISSING"
            and skill.creator.user_id == api_key_user_id
        )
        if not (is_admin or is_owner):
            raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")

    # WIS-902: Tier-aware install rate limit
    caller_tier = _resolve_caller_tier_for_install(db, request)

    # portal_0610 R1 (P0 paywall-bypass closure, §6.6): tier-ACCESS gate.
    # The visibility check above only stops PRIVATE skills. A FREE authenticated
    # key passing that check could still pull a PRO skill's tarball (live-repro'd
    # on prod 2026-06-10: free key → full `chef` tier=pro tarball, HTTP 200).
    # Gate the caller's tier against the skill's tier BEFORE minting any URL.
    # Anonymous free-install callers are already constrained to tier=free skills
    # above (line ~128), so this is the gate for AUTHENTICATED callers.
    if not is_anonymous_free_install:
        from app.authz import tier_rank_allows_install

        if not tier_rank_allows_install(caller_tier, skill.tier):
            from app.tier_labels import display_label as _dl

            raise HTTPException(
                status_code=403,
                detail=(f"This skill requires {_dl(skill.tier or 'pro')} tier. Upgrade to install it."),
            )

    install_limit = TIER_INSTALL_LIMITS.get(caller_tier, 5)
    api_key_id = getattr(request.state, "api_key_id", None)

    if install_limit is not None:  # None = unlimited
        today_count = _count_today_installs(db, api_key_id)
        if today_count >= install_limit:
            remaining = 0
            reset_at = (
                datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            ).isoformat()

            from fastapi.responses import JSONResponse as _JRP

            return _JRP(
                status_code=429,
                content={
                    "detail": f"Install rate limit exceeded ({install_limit}/day for {caller_tier or 'free'} tier). "
                    f"Upgrade to {display_label('pro_plus')} for unlimited installs.",
                    "tier": caller_tier,
                    "limit": install_limit,
                    "remaining": remaining,
                    "reset_at": reset_at,
                },
                headers={
                    "X-RateLimit-Limit": str(install_limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": reset_at,
                    "Retry-After": str(
                        int(
                            (
                                datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
                                + timedelta(days=1)
                                - datetime.now(UTC)
                            ).total_seconds()
                        )
                    ),
                },
            )

    if not skill.versions:
        raise HTTPException(status_code=404, detail=f"No versions available for '{slug}'")

    # Stream 4: explicit version pinning. None ⇒ latest (existing behaviour).
    if version:
        target = next((v for v in skill.versions if v.semver == version), None)
        if target is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Version '{version}' not found for '{slug}'. "
                    f"Available: {[v.semver for v in skill.versions]}"
                ),
            )
        latest = target
    else:
        latest = skill.versions[0]

    # Generate a signed token (HMAC-style with itsdangerous)
    # Issue #24 (secfix_1905/H): salt added so install tokens cannot be
    # reused as tokens for any other URLSafeTimedSerializer in this app.
    # Phase 3+4: primary salt is now "loopskill-install"; verifier falls back
    # to "recipes-skill-install" so in-flight signed URLs still work.  # compat-alias
    from itsdangerous import URLSafeTimedSerializer

    from app import config
    from app.config import settings

    serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="loopskill-install")
    token = serializer.dumps({"slug": slug, "version_id": str(latest.id), "mode": mode})

    # Build signed download URL — use the public origin so installs work
    # from any host (not only loopback). Fall back to localhost for dev.
    public_origin = config.public_origin()
    url_base = public_origin.rstrip("/") + "/api/skills/_download" + "?" + "tok" + "en="
    tarball_url = url_base + token

    # Log install event
    from uuid import uuid4 as _uuid4

    api_key_id = getattr(request.state, "api_key_id", None)
    event = InstallEvent(
        id=_uuid4(),
        skill_id=skill.id,
        skill_slug=slug,
        api_key_id=api_key_id,
        version_semver=latest.semver,
        client_ip=_real_client_ip(request, settings.TRUSTED_PROXY_CIDRS),  # Issue #22
    )
    db.add(event)

    # RCP-13: keep the denormalised Skill.install_count counter in sync with
    # the InstallEvent table (the path /api/skills/install actually writes).
    # Atomic SQL-level expression so concurrent installs cannot lose writes.
    # Same transaction as the InstallEvent insert — either both land or
    # neither does.
    db.query(Skill).filter(Skill.id == skill.id).update(
        {Skill.install_count: Skill.install_count + 1},
        synchronize_session=False,
    )

    # spotify_0608 Ph E — mint a provenance_id for this (direct) install so the
    # agent can pass it to recipes_feedback / recipes_report_skill_error and have
    # the report route to the correct creator repo. The direct path has no
    # bundle context (cookbook_id stays NULL); attribution is 'attributed'  # compat-alias
    # because we know the exact skill + version.
    from app.services.provenance import mint_provenance

    db.flush()  # ensure event.id is populated before the FK insert
    provenance_id = mint_provenance(db, event)
    db.commit()

    # WIS-902: Add rate-limit info headers to successful response
    resp_headers = {}
    if install_limit is not None:
        today_count_after = _count_today_installs(db, api_key_id)
        remaining = max(0, install_limit - today_count_after)
        resp_headers["X-RateLimit-Limit"] = str(install_limit)
        resp_headers["X-RateLimit-Remaining"] = str(remaining)

    resp = InstallResponse(
        slug=slug,
        version=latest.semver,
        tarball_url=tarball_url,
        checksum_sha256=latest.checksum_sha256,
        size_bytes=latest.tarball_size_bytes,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        manifest=_build_manifest(latest, skill),
        provenance_id=provenance_id,
    )
    if resp_headers or ref:
        from fastapi.responses import JSONResponse as _JR

        json_resp = _JR(content=resp.model_dump(mode="json"), headers=resp_headers)
        _set_utm_ref_cookie(json_resp, ref, db=db)
        return json_resp
    return resp


def _verify_signed_token(token: str, *, secret: str, max_age: int = 3600) -> dict:
    """Verify a signed install token, trying new salt then falling back to old.

    Phase 3+4 dual-salt: primary salt is "loopskill-install"; tokens signed
    with the old "recipes-skill-install" salt are still accepted so in-flight  # compat-alias
    URLs from before the rename continue to work.
    """
    from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

    for salt in ("loopskill-install", "recipes-skill-install"):  # compat-alias
        try:
            return URLSafeTimedSerializer(secret, salt=salt).loads(token, max_age=max_age)
        except SignatureExpired:
            raise HTTPException(status_code=410, detail="Download token expired")
        except BadSignature:
            continue
    raise HTTPException(status_code=403, detail="Invalid download token")


@router.get("/skills/_download", tags=["skills"])
def download_tarball(
    token: str = Query(..., description="Signed download token"),
    db: Session = Depends(get_db),
):
    """Verify signed token and return tarball info."""
    from app.config import settings

    data = _verify_signed_token(token, secret=settings.SIGNING_SECRET)

    slug = data["slug"]
    version_id = data["version_id"]

    # The signed token round-trips version_id as a STRING (json). SkillVersion.id
    # is UUID(as_uuid=True); on SQLite (the self-host path) the type adapter calls
    # .hex on the bound value and raises 'str object has no attribute hex' for a
    # raw string — Postgres happens to coerce it, so this only bites self-hosters.
    # Coerce defensively (accept already-UUID too) before the query.
    from uuid import UUID as _UUID

    if isinstance(version_id, str):
        try:
            version_id = _UUID(version_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Malformed version id in token") from exc

    version = db.query(SkillVersion).filter(SkillVersion.id == version_id).first()
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")

    # Stream the actual tarball file. Path is recorded at publish-time as
    # absolute (e.g. /var/lib/recipes-skills/agent-rescue/1.1.0.tar.gz).
    import pathlib as _pl

    from fastapi.responses import FileResponse

    tar_path = _pl.Path(version.tarball_path) if version.tarball_path else None
    if not tar_path or not tar_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Tarball missing on disk for {slug}@{version.semver}",
        )
    return FileResponse(
        path=str(tar_path),
        media_type="application/gzip",
        filename=f"{slug}-{version.semver}.tar.gz",
        headers=_immutable_cache_headers(version.checksum_sha256),
    )
