"""Shared helper functions for skill-related route handlers.

Extracted from app/routes.py (Phase E — secfix_1905) for clean module
boundaries. Pure functions with no FastAPI surface — safe to import from
any module without creating circular dependencies.

Backwards-compatible: ``from app.routes import _build_manifest`` continues
to work via re-exports in routes.py for one release window (tracked for
removal in secfix_1906).
"""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models import (
    APIKey,
    InstallEvent,
    Skill,
    User,
)
from app.schemas import SkillOut

if TYPE_CHECKING:
    from app.models import SkillVersion

# ── UTM ref attribution constants ──────────────────────────────────────────
_UTM_REF_ALLOWLIST = frozenset({"li", "x", "yt", "ig", "fb", "agentpact"})
_UTM_COOKIE_NAME = "recipes_utm_ref"
_UTM_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days in seconds

# Related skills cap — Stage 1 contract (10 items max)
RELATED_SKILLS_CAP = 10

# Graph rail cap — Stage 2 (G16) cap
GRAPH_RAIL_CAP = 10


def _build_manifest(latest: SkillVersion, skill: Skill) -> dict:
    """F-API-14: Build manifest dict from skill.toml for install response."""
    toml_text = latest.skill_toml or ""
    try:
        toml_data = tomllib.loads(toml_text).get("skill", {})
        return {
            "category": toml_data.get("category") or skill.category,
            "tags": toml_data.get("tags", []),
            "tier": toml_data.get("tier"),
        }
    # Rationale: TOML may be malformed or empty; any parse/key error → safe defaults
    except Exception:  # noqa: BLE001
        return {"category": skill.category}


def _skill_to_out(
    skill: Skill,
    install_count_total: int = 0,
    install_count_7d: int = 0,
) -> SkillOut:
    """Convert a Skill ORM object to a SkillOut schema instance."""
    latest = skill.versions[0].semver if skill.versions else None
    return SkillOut(
        id=skill.id,
        slug=skill.slug,
        title=skill.title,
        description=skill.description,
        category=skill.category,
        tier=skill.tier,
        is_public=skill.is_public,
        creator_name=skill.creator.name if skill.creator else None,
        creator_handle=skill.creator.handle if skill.creator else None,
        creator_url=skill.creator.url if skill.creator else None,
        latest_version=latest,
        install_count_total=install_count_total,
        install_count_7d=install_count_7d,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
        last_verified=getattr(skill, "last_verified", None),
        quality_score=getattr(skill, "quality_score", None),
    )


def _install_counts_for(db: Session, skill_ids: list) -> dict:
    """Return {skill_id: (total, last_7d)} for the supplied skill ids.

    One round-trip aggregation — small marketplace (≤200 skills) so a
    grouped query is cheaper than a LATERAL per row.

    spotify_0608 Ph B (§4.2): EXCLUDES synthetic installs. An install counts as
    organic when its api_key_id is NULL (anonymous) OR its APIKey.is_test is
    false. Only installs explicitly keyed to a test/CI/internal key are dropped.
    This keeps the public-ranking / leaderboard / GTM-signal counts honest.
    """
    if not skill_ids:
        return {}
    since_7d = datetime.now(UTC) - timedelta(days=7)
    rows = (
        db.query(
            InstallEvent.skill_id,
            func.count(InstallEvent.id).label("total"),
            func.sum(case((InstallEvent.created_at >= since_7d, 1), else_=0)).label("last_7d"),
        )
        .outerjoin(APIKey, APIKey.id == InstallEvent.api_key_id)
        .filter(
            InstallEvent.skill_id.in_(skill_ids),
            # Anonymous installs (no key) are organic; keyed installs are organic
            # unless the key is flagged is_test. coalesce so NULL→organic.
            func.coalesce(APIKey.is_test, False).is_(False),
        )
        .group_by(InstallEvent.skill_id)
        .all()
    )
    return {sid: (int(total or 0), int(last_7d or 0)) for sid, total, last_7d in rows}


def _cookbook_install_counts(db: Session, cookbook_id) -> tuple[int, int]:
    """Return (total, last_7d) installs ATTRIBUTED TO this cookbook.

    portal_0610 R7: the public cookbook card previously summed each member
    skill's GLOBAL install count, so a skill shared across N cookbooks had its
    installs counted N times (e.g. super-memory's ~1520 installs added to every
    cookbook containing it — the marketplace-wide sum ran ~1.86× actual). That
    overstates a cookbook's reach and is a GTM-trust problem.

    The honest count is "installs that came THROUGH this cookbook" — InstallEvent
    rows stamped with this cookbook_id (the cookbook install paths set it via
    provenance). Organic-only: the same §4.2 is_test exclusion as
    ``_install_counts_for``. A cookbook whose skills were all installed via the
    direct /api/skills/install path (cookbook_id NULL) correctly shows 0 — those
    installs were not attributable to the cookbook.
    """
    since_7d = datetime.now(UTC) - timedelta(days=7)
    row = (
        db.query(
            func.count(InstallEvent.id).label("total"),
            func.sum(case((InstallEvent.created_at >= since_7d, 1), else_=0)).label("last_7d"),
        )
        .outerjoin(APIKey, APIKey.id == InstallEvent.api_key_id)
        .filter(
            InstallEvent.cookbook_id == cookbook_id,
            func.coalesce(APIKey.is_test, False).is_(False),
        )
        .one()
    )
    return int(row.total or 0), int(row.last_7d or 0)


def _resolve_ref_value(ref: str | None, db: Session | None = None) -> str | None:
    """Normalise an inbound ``?ref=`` value to a storable attribution token.

    portal_0610 R2: previously only platform codes ({li,x,yt,ig,fb,agentpact})
    were accepted; a creator-handle or owner-UUID ref (the value the public
    cookbook card emits) never matched the allowlist and was SILENTLY DROPPED,
    so the "install attribution visible from week 1" promise was false.

    Resolution order:
      * a known platform code → stored as-is (e.g. "x").
      * a value resolving to a real Creator.handle (requires ``db``) → stored as
        ``creator:<handle>`` so creator attribution is namespaced away from
        platform codes and can be reported distinctly.
      * anything else (unknown handle, malformed) → None (dropped, as before —
        never store an unvalidated free-text ref).
    """
    if not ref:
        return None
    if ref in _UTM_REF_ALLOWLIST:
        return ref
    if db is not None:
        from app.models import Creator

        # Accept either a bare handle or an already-namespaced "creator:<handle>".
        handle = ref.split("creator:", 1)[1] if ref.startswith("creator:") else ref
        exists = db.query(Creator.id).filter(Creator.handle == handle).first()
        if exists is not None:
            return f"creator:{handle}"
    return None


def _set_utm_ref_cookie(response, ref: str | None, db: Session | None = None) -> None:
    """Set httpOnly UTM ref cookie if ref resolves to a valid attribution token.

    portal_0610 R2: now resolves creator-handle refs (via ``_resolve_ref_value``)
    in addition to platform codes. Passing ``db`` enables handle validation; with
    no db only platform codes are accepted (backward-compatible).
    """
    resolved = _resolve_ref_value(ref, db=db)
    if resolved:
        response.set_cookie(
            _UTM_COOKIE_NAME,
            value=resolved,
            max_age=_UTM_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=True,
        )


def _count_today_installs(db: Session, api_key_id) -> int:
    """Count installs today for a given API key ID."""
    if api_key_id is None:
        return 0
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(func.count(InstallEvent.id))
        .filter(
            InstallEvent.api_key_id == api_key_id,
            InstallEvent.created_at >= today_start,
        )
        .scalar()
        or 0
    )


def _resolve_related(db: Session, skill: Skill) -> list:
    """Resolve `skill.related_skills` slug list to public SkillOut payloads.

    Filters applied (Stage 1 contract):
      - drop self-reference (skill.slug appearing in its own related_skills)
      - drop slugs that don't exist in DB
      - drop is_public=False skills (no internal-leak)
      - cap at RELATED_SKILLS_CAP, preserving frontmatter declaration order
    """
    raw = skill.related_skills or []
    if not raw:
        return []

    # Normalise: drop self-refs, lowercase, dedupe preserving order
    seen: set[str] = set()
    candidates: list[str] = []
    for s in raw:
        if not isinstance(s, str):
            continue
        norm = s.strip().lower()
        if not norm or norm == skill.slug or norm in seen:
            continue
        seen.add(norm)
        candidates.append(norm)
        if len(candidates) >= RELATED_SKILLS_CAP * 2:  # over-fetch buffer for filtering
            break

    if not candidates:
        return []

    # Single query: pull all candidate public skills at once
    # secfix_1905/I-followup: also exclude archived skills.
    rows = (
        db.query(Skill)
        .filter(Skill.slug.in_(candidates), Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
        .all()
    )
    by_slug = {r.slug: r for r in rows}

    # Preserve declaration order, cap at limit
    out = []
    for slug in candidates:
        r = by_slug.get(slug)
        if not r:
            continue
        latest = r.versions[0].semver if r.versions else None
        out.append(
            {
                "id": r.id,
                "slug": r.slug,
                "title": r.title,
                "description": r.description,
                "category": r.category,
                "tier": r.tier,
                "is_public": r.is_public,
                "creator_name": r.creator.name if r.creator else None,
                "creator_handle": r.creator.handle if r.creator else None,
                "creator_url": r.creator.url if r.creator else None,
                "latest_version": latest,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
        )
        if len(out) >= RELATED_SKILLS_CAP:
            break
    return out


def _hydrate_skill_outs(db: Session, slugs: list[str]) -> list[dict]:
    """Resolve a list of slugs (preserving order) to public SkillOut dicts."""
    if not slugs:
        return []
    # secfix_1905/I-followup: also exclude archived skills.
    rows = (
        db.query(Skill)
        .filter(Skill.slug.in_(slugs), Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
        .all()
    )
    by_slug = {r.slug: r for r in rows}
    out = []
    for slug in slugs:
        r = by_slug.get(slug)
        if not r:
            continue
        latest = r.versions[0].semver if r.versions else None
        out.append(
            {
                "id": r.id,
                "slug": r.slug,
                "title": r.title,
                "description": r.description,
                "category": r.category,
                "tier": r.tier,
                "is_public": r.is_public,
                "creator_name": r.creator.name if r.creator else None,
                "creator_handle": r.creator.handle if r.creator else None,
                "creator_url": r.creator.url if r.creator else None,
                "latest_version": latest,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
        )
    return out


def _resolve_caller_tier_for_install(db: Session, request) -> str | None:
    """Resolve caller tier from request.state (set by APIKeyMiddleware).

    Returns the user's subscription tier, or None for anonymous/master.
    Master key gets unlimited installs (treated as pro_plus tier).
    """
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    # Master key gets unlimited installs (treated as pro_plus tier).
    if api_key_user_id is None:
        return "pro_plus"
    if api_key_user_id == "MISSING" or api_key_user_id == "CBT_TOKEN":
        return None

    user = db.query(User).filter(User.id == api_key_user_id).first()
    if not user or user.subscription_status not in ("active", "trialing"):
        return None
    return user.subscription_tier


def _resolve_cookbook_owner_tier(db: Session, cookbook) -> str | None:
    """Resolve the effective install tier for a cookbook's OWNER.

    portal_0610 R1 / §6.7-L10. Cookbook install paths gate tier-access on the
    COOKBOOK OWNER's subscription, not the calling agent's — because the owner
    (the paying customer) is who delivered the cookbook to a client agent via a
    cbt_ share-token. A free-owner cookbook can therefore never hand out a Pro
    skill, while a Pro-owner cookbook can — exactly the L10 ``tier_wide``
    enforcement contract, resolved server-side at install time.

    Returns:
        - ``"pro_plus"`` for an owner-less / base catalog cookbook
          (cookbook_owner is None): the WiseChef-curated catalog is not gated
          by a personal subscription. Skill-level visibility still applies.
        - the owner's ``subscription_tier`` when their subscription is
          active/trialing.
        - ``"free"`` when the owner exists but has no active paid subscription
          (lapsed Pro must not keep leaking Pro skills to client agents).
    """
    owner_id = getattr(cookbook, "cookbook_owner", None)
    if owner_id is None:
        # Owner-less base/curated catalog — not personally gated.
        return "pro_plus"
    owner = db.query(User).filter(User.id == owner_id).first()
    if not owner or owner.subscription_status not in ("active", "trialing"):
        return "free"
    return owner.subscription_tier or "free"


# ── Install-event recording (denormalised counter sync) ────────────────────
#
# Shared by every install-producing route so all paths (single-skill /api/skills/install,
# cookbook bulk install, cookbook single-skill install, MCP recipes_cookbook_install)
# write an InstallEvent row AND bump Skill.install_count in the same transaction.
#
# Before recipes-D, only /api/skills/install recorded events. Cookbook-share installs
# (the only path cbt_-token holders can use) were invisible in transparency stats —
# install_count and InstallEvent.skill_id both stayed empty for those skills. The
# Varys end-to-end install (cookbook_share_2105 OUTCOME, 2026-05-25) was the first
# concrete demonstration of the gap: 5 skills installed, 0 events recorded.


def _record_install_event(
    db: Session,
    *,
    skill: Skill,
    version_semver: str,
    request=None,
    source: str = "cookbook",
) -> None:
    """Insert an InstallEvent and atomically bump Skill.install_count.

    Same shape as install_routes.recipes_install does for the single-skill path,
    factored out so cookbook and MCP install paths produce identical records.

    Args:
        db: Active SQLAlchemy session. Caller owns commit/rollback so the
            event lands in the same transaction as any payload mutation that
            triggered it.
        skill: The Skill being installed. ``skill.id`` and ``skill.slug`` are
            read; nothing on the ORM object is mutated.
        version_semver: SemVer string of the installed version. Recorded as-is.
        request: Optional FastAPI Request — when present, ``api_key_id`` and
            ``client_ip`` are extracted from request state. Omit for MCP-tool
            callers that have no HTTP request bound.
        source: Where the install was triggered from. One of:
            - ``"direct"``  — /api/skills/install (canonical single-skill path)
            - ``"cookbook"`` — POST /api/cookbooks/{id}/install or single-skill
              install via cookbook prefix
            - ``"mcp"``     — recipes_cookbook_install MCP tool
            Stored as a tag on the event row's ``api_key_id`` metadata via
            future schema extension; today it parameterises which install path
            wrote the row for observability without requiring a schema change.

    Notes:
        Caller must commit after this returns. The function does NOT commit so
        it composes cleanly with multi-skill bulk operations that want all
        events in one transaction.
    """
    from uuid import uuid4 as _uuid4

    api_key_id = None
    client_ip = None
    if request is not None:
        api_key_id = getattr(request.state, "api_key_id", None)
        # Defer the trusted-proxy IP extraction; cookbook routes don't import it.
        try:
            from app.config import settings
            from app.utils.client_ip import _real_client_ip

            client_ip = _real_client_ip(request, settings.TRUSTED_PROXY_CIDRS)
        # Rationale: client_ip is observability-only; never fail the install
        # because IP parsing tripped. Same conservative posture as the
        # /api/skills/install path (Issue #22 fix did not raise on parse fail).
        except Exception:  # noqa: BLE001
            client_ip = None

    event = InstallEvent(
        id=_uuid4(),
        skill_id=skill.id,
        skill_slug=skill.slug,
        api_key_id=api_key_id,
        version_semver=version_semver,
        client_ip=client_ip,
    )
    db.add(event)

    # spotify_0608 Ph B (§4.2 install-count integrity): the InstallEvent row is
    # ALWAYS written (audit/provenance), but the denormalized Skill.install_count
    # — which feeds the carousel popularity term and other public-ranking surfaces
    # — is bumped ONLY for organic installs. A test/CI/internal key (is_test=true)
    # records the event but does NOT inflate the public counter. Anonymous installs
    # (no key) are organic and DO bump.
    if api_key_id is not None:
        is_test = db.query(APIKey.is_test).filter(APIKey.id == api_key_id).scalar()
        if is_test:
            return

    # Atomic SQL-level bump — concurrent installs cannot lose writes.
    # Same pattern as install_routes.recipes_install (RCP-13).
    db.query(Skill).filter(Skill.id == skill.id).update(
        {Skill.install_count: Skill.install_count + 1},
        synchronize_session=False,
    )


# ── Retired skill registry ───────────────────────────────────────────────────
# WIS-903: Lazy-loaded, cached set of retired skill slugs.
# Consolidated here (Phase L — topshelf_2605) so all route modules share one
# loader instead of duplicating file-parse logic at import time.

from pathlib import Path as _Path
from functools import lru_cache as _lru_cache

_RETIREMENT_FILE = _Path(__file__).resolve().parent.parent / "retired-skills.txt"


@_lru_cache(maxsize=1)
def get_retired_set() -> dict[str, str]:
    """Return {slug: redirect_url} for all retired skills.

    Cached after the first call. The file is parsed lazily so import-time
    side-effects are eliminated. Returns an empty dict when the file is absent.
    """
    result: dict[str, str] = {}
    if not _RETIREMENT_FILE.exists():
        return result
    for line in _RETIREMENT_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            parts = line.split(None, 1)
            if len(parts) == 2:
                result[parts[0]] = parts[1]
    return result
