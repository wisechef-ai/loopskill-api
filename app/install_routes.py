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
from uuid import UUID

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
from app.models import Bundle, BundleSkill, Skill, SkillVersion
from app.schemas import InstallResponse
from app.tier_labels import display_label

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

# bundles_0811 P3 follow-up — accepted prefixes for a `<prefix>:<slug>` install
# ref against the federated hub.
#
# `hermes-hub` is the HUB NAMESPACE: all 90,605 federation_hub_skills rows live
# under it whatever their origin. The others are the `upstream_source` values a
# user actually SEES on a search result card, so typing what you were shown must
# work. Verified on prod 2026-08-11: distinct upstream_source = browse-sh,
# claude-marketplace, clawhub, github, lobehub, official, skills-sh — and
# `hermes-hub` is deliberately NOT one of them.
#
# Safe because hub slugs are globally unique (90,605 rows / 90,605 distinct
# slugs), so the prefix is a hint, never a disambiguator. `ext:` is excluded by
# the caller: it names a MATERIALIZED local pointer row, not a hub row.
_FEDERATED_SLUG_PREFIXES: frozenset[str] = frozenset(
    {
        "hermes-hub",
        "browse-sh",
        "claude-marketplace",
        "clawhub",
        "github",
        "github-oss",
        "lobehub",
        "official",
        "skills-sh",
    }
)


def _resolve_validated_bundle(db: Session, bundle_id: str, skill: Skill) -> UUID:
    """Validate a caller-supplied bundle_id for mesh0408 Q-031.

    The supplied bundle must:
      - parse as a UUID -> else 422
      - exist -> else 404 (mirrors ``_resolve_owned_cookbook``'s no-existence-leak
        precedent in app/bundle_routes.py — an unknown id and a real-but-foreign
        id are indistinguishable to the caller)
      - actually CONTAIN the skill being installed -> else 422. Without this a
        caller could attribute an install to an unrelated bundle and misroute
        someone else's defect reports to that bundle's curator repo. This is a
        content-membership failure, not an existence question, so it is 422
        (unprocessable — the request is well-formed and the bundle is real, but
        the combination is invalid) rather than 404.

    Deliberately does NOT check bundle OWNERSHIP — installing FROM a public (or
    any readable) bundle is a normal, unauthenticated-adjacent action distinct
    from cookbook CRUD, which is why this does not reuse
    ``_resolve_owned_cookbook``. Only membership (does the bundle actually
    contain this skill) is enforced, per the task brief.
    """
    try:
        bid = UUID(bundle_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Malformed bundle_id; must be a UUID")

    bundle = db.query(Bundle).filter(Bundle.id == bid).first()
    if bundle is None:
        raise HTTPException(status_code=404, detail="Bundle not found")

    member = (
        db.query(BundleSkill)
        .filter(
            BundleSkill.bundle_id == bid,
            BundleSkill.skill_id == skill.id,
            BundleSkill.source != "disabled",
        )
        .first()
    )
    if member is None:
        raise HTTPException(
            status_code=422,
            detail="Bundle does not contain this skill; cannot attribute the install to it",
        )
    return bid


def _install_federated_hermes_hub_ref(fed_slug: str, db: Session):
    """Resolve `hermes-hub:<slug>` to an install INSTRUCTION. Never bytes.

    bundles0811 P3 item 3. Looks up the FederationHubSkill row by slug for
    its repo/path (the row's TRUE coordinates), then resolves a fetchable
    SKILL.md URL via the P3 instruction resolver — a plain string, never a
    fetched body. LoopSkill stores/fetches zero federated bytes here.

    Returns a raw ``JSONResponse`` (bypassing ``InstallResponse``'s
    required version/tarball_url fields, which a federated entry has no
    tarball/version for) in the same key shape as
    ``resolve_external_install`` (bundle_external.py) plus an
    ``install_command`` so an agent has a copy-paste path, matching the
    public ``/skills/external/{source}/{slug}/install`` route's contract.
    """
    from fastapi.responses import JSONResponse

    from app.models import FederationHubSkill
    from app.services.federation_hub_install import resolve_install_instruction

    hub_row = db.query(FederationHubSkill).filter(FederationHubSkill.slug == fed_slug).first()
    if hub_row is None:
        raise HTTPException(status_code=404, detail=f"External skill '{fed_slug}' not found in hermes-hub")

    # bundles0811 P3 item 4 (Q3): licence is RECORDED when the source's
    # ingested `extra` payload carries one, never used to gate anything
    # below — an unknown/missing licence resolves EXACTLY the same
    # instruction as any other. No branch in this function reads it.
    hub_extra = hub_row.extra if isinstance(hub_row.extra, dict) else {}
    recorded_license = hub_extra.get("license") if hub_extra else None

    instr = resolve_install_instruction(
        repo=hub_row.repo,
        path=hub_row.path,
        origin_url=hub_row.origin_url,
        slug=fed_slug,
        license=recorded_license,
    )
    if not instr.url:
        raise HTTPException(status_code=404, detail=f"External skill '{fed_slug}' has no resolvable location")

    leaf = fed_slug.rsplit("--", 1)[-1].rsplit("/", 1)[-1]
    payload: dict = {
        "slug": f"hermes-hub:{fed_slug}",
        "source": "hermes-hub",
        "namespace": "external",
        "quality": "community · as-is",
        **instr.to_dict(),
    }
    if instr.kind == "fetch":
        payload["install_command"] = (
            f"mkdir -p ~/.claude/skills/{leaf} && curl -fsSL {instr.url} -o ~/.claude/skills/{leaf}/SKILL.md"
        )
    else:
        payload["install_command"] = None
        payload["agent_instructions"] = (
            "LoopSkill did not resolve a direct SKILL.md location for this skill. "
            f"Visit the origin to find it yourself: {instr.url}"
        )
    return JSONResponse(content=payload)


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
    bundle_id: str | None = Query(
        None,
        description=(
            "Optional bundle (cookbook) the install is attributed to. Must be a "
            "valid UUID for a bundle that actually contains this skill. Stamps "
            "provenance so a later loopskill_feedback/skill-error report routes to "
            "the bundle curator's configured repo. Omitted → direct install, "
            "bundle_id stays NULL on the InstallEvent (today's behaviour)."
        ),
    ),
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

    # bundles0811 P3 item 3 — a `<source>:<slug>` ref (e.g.
    # "hermes-hub:1password") names a FEDERATED entry, not a local Skill row.
    # No local Skill can ever legitimately carry a colon in its slug (the
    # only colon-bearing convention in this codebase is the `ext:` prefix on
    # a MATERIALIZED pointer row, which is excluded below), so this check is
    # safe to run before the local-Skill lookup rather than only as a 404
    # fallback. Resolved via the P3 instruction resolver — zero bytes
    # fetched or stored here, unlike the bundle-scoped fetch-origin routes.
    # bundles_0811 P3 follow-up: `hermes-hub` is the HUB NAMESPACE, not an
    # upstream source. Every one of the 90,605 federation_hub_skills rows lives
    # under it regardless of where it came from (verified on prod: distinct
    # upstream_source = browse-sh, claude-marketplace, clawhub, github, lobehub,
    # official, skills-sh — `hermes-hub` is not among them), and slugs are
    # globally unique (90,605 rows / 90,605 distinct slugs).
    #
    # So `hermes-hub:<slug>` already resolves a skills-sh or github row — but a
    # user who read `source: "skills-sh"` off a search result and typed
    # `skills-sh:<slug>` got a bare "Skill not found", which reads as "we don't
    # have it" when we do. Because slugs are globally unique, accepting any
    # known upstream source as an ALIAS for the hub namespace is unambiguous.
    if ":" in slug and not slug.startswith("ext:"):
        fed_source, _sep, fed_slug = slug.partition(":")
        if fed_source in _FEDERATED_SLUG_PREFIXES:
            return _install_federated_hermes_hub_ref(fed_slug, db)

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

    # mesh0408 Q-031: validate the caller-supplied bundle_id (if any) BEFORE
    # any other work. Do not trust it — see _resolve_validated_bundle.
    validated_bundle_id: UUID | None = None
    if bundle_id is not None:
        validated_bundle_id = _resolve_validated_bundle(db, bundle_id, skill)

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
                detail="Share tokens can only access bundle routes",
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

    # mesh0408 Q-031: route through the canonical provenance-minting entry
    # point (app/services/provenance.py::record_install_with_provenance) —
    # the SAME helper app/bundle_routes.py already uses for cookbook installs
    # — instead of hand-rolling a parallel InstallEvent + counter-bump + mint
    # path. This fixes two things at once:
    #   1. bundle_id is now stamped when the caller supplies a validated
    #      bundle_id, so the feedback rail (route_targets_for_provenance ->
    #      _curator_target) can actually resolve a curator repo for a
    #      bundle-scoped install (previously: always NULL, always unroutable).
    #   2. the install_count counter now respects the is_test integrity rule
    #      (Ph B §4.2) — a test/CI key records the InstallEvent but does NOT
    #      inflate the public counter. The old hand-rolled block bumped the
    #      counter unconditionally, inflating it with test-key traffic.
    from app.services.provenance import ATTR_ATTRIBUTED, record_install_with_provenance

    api_key_id = getattr(request.state, "api_key_id", None)
    _event, provenance_id = record_install_with_provenance(
        db,
        skill=skill,
        version_semver=latest.semver,
        request=request,
        source="cookbook" if validated_bundle_id is not None else "direct",
        cookbook_id=validated_bundle_id,
        attribution=ATTR_ATTRIBUTED,
        commit=True,
    )

    # WIS-902: Add rate-limit info headers to successful response
    resp_headers = {}
    if install_limit is not None:
        today_count_after = _count_today_installs(db, api_key_id)
        remaining = max(0, install_limit - today_count_after)
        resp_headers["X-RateLimit-Limit"] = str(install_limit)
        resp_headers["X-RateLimit-Remaining"] = str(remaining)

    # bhint0823 (t_8ccbdbc5) — bundle fast-path onboarding hint. Only direct
    # installs can be "hand-walking a bundle" (a bundle-attributed install is
    # already on the fast path), and the trigger requires >=3 direct installs
    # from this IP in 24h, so the common case pays nothing: compute_bundle_hint
    # returns None on the client_ip-missing branch before any query runs.
    # Fail-quiet by design — a hinting regression must never 500 an install.
    bundle_hint = None
    if validated_bundle_id is None:
        # Rationale: observability/onboarding hint only; an internal failure
        # (DB hiccup mid-hint) must not fail an install that already committed.
        try:
            from app.services.bundle_hint import compute_bundle_hint

            bundle_hint = compute_bundle_hint(db, client_ip=getattr(_event, "client_ip", None))
        except Exception:  # noqa: BLE001
            bundle_hint = None
    if bundle_hint is not None:
        resp_headers["X-LoopSkill-Bundle-Hint"] = bundle_hint["slug"]

    resp = InstallResponse(
        slug=slug,
        version=latest.semver,
        tarball_url=tarball_url,
        checksum_sha256=latest.checksum_sha256,
        size_bytes=latest.tarball_size_bytes,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        manifest=_build_manifest(latest, skill),
        provenance_id=provenance_id,
        bundle_hint=bundle_hint,
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
