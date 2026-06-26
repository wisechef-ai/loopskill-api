"""Cookbook CRUD endpoints — v7 Phase B.

Endpoints (all gated to subscription_tier in {'pro','pro_plus'} OR master key):
Legacy slugs 'cook'/'operator' accepted via _is_paid_tier/_is_pro_plus_tier shims for 30 days.
  - POST   /api/cookbooks                       create (per-tier cap via SSOT)
  - GET    /api/cookbooks                       list mine
  - GET    /api/cookbooks/{id}                  detail with skills
  - POST   /api/cookbooks/{id}/skills           add skill (validates slug)
  - DELETE /api/cookbooks/{id}/skills/{slug}    soft-delete (source='disabled')
  - POST   /api/cookbooks/{id}/install          idempotent install payload
  - GET    /api/cookbooks/{id}/manifest         YAML manifest
  - GET    /api/cookbooks/{id}/sync             since-filter event log

Tier gate: middleware stamps api_key_user_id on request.state. The static master
key bypasses tier checks. Free / no-tier users receive 401 on create. The
per-tier cookbook cap is the SSOT in config/tiers.yaml (read via
tier_labels.cookbook_limit); a 403 fires when a user is at their tier's limit.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app import config
from app.database import get_db
from app.models import Bundle, BundleSkill, Skill, SkillVersion, User
from app.services.bundle_external import (
    install_descriptor_for,
    is_external_skill,
    resolve_external_install,
)
from app.tier_labels import cookbook_limit

logger = logging.getLogger(__name__)
_h = APIRouter(tags=["bundles"])  # Phase 3+4: handlers registered prefix-free; combined below

# RCP-INCIDENT-2026-05-11: COOKBOOK_TIERS and UNLIMITED_TIERS now use helper
# functions (_is_paid_tier, _is_operator_tier) defined in tier_labels.py which
# transparently accept the legacy 'studio' slug for 30 days. These set
# constants remain for reference/documentation only — do not use for gate checks.
COOKBOOK_TIERS = {"pro", "pro_plus"}  # canonical; legacy slugs handled via shim
UNLIMITED_TIERS = {"pro_plus"}  # canonical; legacy slugs handled via shim
ACTIVE_SUB_STATUSES = {"active", "trialing"}
ALLOWED_SOURCES = {"forked", "custom-added", "overridden", "disabled"}

# WIS-902: Pro tier skill cap per bundle
COOK_SKILL_CAP = 25


def _touch_bundle_generation(db: Session, cookbook_id: UUID) -> None:  # compat-alias
    """Advance a cookbook's generation token (Cookbook.updated_at).

    evergreen_0206 Phase A — the cheap-poll generation token.

    SQLAlchemy's ``onupdate=func.now()`` on ``Cookbook.updated_at`` fires ONLY
    when the parent ``cookbooks`` row is UPDATEd — never when a child
    ``CookbookSkill`` row is added, removed, or re-pinned. That made the
    generation token lie: a cookbook's declared skill set could change while
    its ``updated_at`` stayed frozen, so a subscribed agent polling with
    ``If-None-Match: <generation>`` would get a false 304 and never reconcile.

    Every code path that mutates a cookbook's declared skill set MUST call this
    so the generation token is truthful. This is the load-bearing primitive
    behind the 304-fast-path (Phase D) and subscribe-not-poll fan-out.

    Uses ``func.now()`` (DB-side clock) for a single source of time truth,
    consistent with the column's ``server_default``/``onupdate``.
    """
    db.query(Bundle).filter(Bundle.id == cookbook_id).update(
        {"updated_at": func.now()}, synchronize_session=False
    )


# ── CBT scope enforcement for bundle routes ─────────────────────────────


def _enforce_cbt_scope_for_cookbook_route(request: Request, cookbook_id: str) -> None:
    """Enforce cbt_ token scope for cookbook-level routes.

    Raises 403 if:
      - cbt_ token's cookbook_id != route's cookbook_id
      - cbt_ token scope is 'read' and method is not GET
    No-op if no cbt_ token is present (rec_ key path).

    cookbook_share_2105 Phase D — vocabulary expanded:
      scope ∈ {read, edit, install}
      read    → GET only
      edit    → all cookbook operations (current behaviour)
      install → GET + POST /install (read + bulk install). Cannot add/remove
                skills, cannot create child tokens — narrow on purpose. Used
                by "share my cookbook with another agent" flows so the
                recipient can install but not modify.
    """
    scope = getattr(request.state, "cookbook_token_scope", None)
    if scope is None:
        return  # No cbt_ token; rec_ key path

    token_cb_id = getattr(request.state, "cookbook_token_cookbook_id", None)
    try:
        cid = UUID(cookbook_id)
    except (ValueError, TypeError):
        return  # Let downstream handle invalid ID

    if token_cb_id != cid:
        raise HTTPException(
            status_code=403,
            detail="Token scope mismatch (wrong cookbook)",
        )

    if scope == "read" and request.method != "GET":
        # cookbook_share_2105 Phase D: clearer scope-insufficient message.
        # Kept as a plain string (not a dict) so existing clients that read
        # ``resp.json()["detail"]`` as text continue to work — see
        # test_cbt_read_token_blocks_skill_add. The "SCOPE_INSUFFICIENT"
        # token is included in-line so programmatic callers can grep for it.
        raise HTTPException(
            status_code=403,
            detail="SCOPE_INSUFFICIENT: token scope 'read' insufficient; need 'install' or higher",
        )

    if scope == "install":
        # install scope: GET + POST /install only. Block any other mutation.
        path = request.url.path
        is_install_route = path.endswith("/install") or "/install" in path
        if request.method != "GET" and not is_install_route:
            raise HTTPException(
                status_code=403,
                detail=(
                    "SCOPE_INSUFFICIENT: token scope 'install' permits GET + /install only; "
                    "need 'edit' for cookbook modification"
                ),
            )

    # SECURITY: cbt_ tokens NEVER authorize publishing, regardless of scope.
    # Even if a /api/cookbooks/{id}/_publish route is added in the future,
    # this gate blocks it. Same for any path containing /_publish.
    if "/_publish" in request.url.path:
        raise HTTPException(
            status_code=403,
            detail="Share tokens cannot authorize publishing",
        )


# ── Tier gate ────────────────────────────────────────────────────────────


class CookbookCtx(BaseModel):
    user_id: UUID | None = None
    is_master: bool = False
    tier: str | None = None
    # SECURITY: when populated, this caller authenticated via a cbt_ share token
    # scoped to this single bundle. Route-level checks must enforce that any
    # cb the request acts on equals this value, and must block writes if scope='read'.
    cbt_cookbook_id: UUID | None = None

    model_config = {"arbitrary_types_allowed": True}


def require_cookbook_tier(request: Request, db: Session = Depends(get_db)) -> CookbookCtx:
    """Resolve the cookbook auth context for any AUTHENTICATED user.

    evergreen_0206 Phase G OPENS the free on-ramp (decision #3/#10). Previously
    this raised 401 for any non-paid tier — the free funnel was closed. Now any
    authenticated user (free included) passes; the per-tier QUANTITY is enforced
    downstream by the cookbook-count cap (free=1, SSOT) and the maintenance
    conversion gates (free=1 manual sync, cron=Pro, fleet=Pro+). A genuinely
    unauthenticated caller still gets 401.

    SECURITY: cbt_ share tokens stamp api_key_user_id="CBT_TOKEN" (sentinel)
    rather than None — None is the master-key signal. Without this guard a cbt_
    token would inherit master-tier access.
    """
    is_cbt = getattr(request.state, "is_cbt_token", False)
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")

    # cbt_ token: no user, not master. The route-level scope checks gate access.
    if is_cbt or api_key_user_id == "CBT_TOKEN":
        cookbook_id = getattr(request.state, "cookbook_token_cookbook_id", None)
        return CookbookCtx(user_id=None, is_master=False, tier="pro", cbt_cookbook_id=cookbook_id)

    if api_key_user_id is None:
        return CookbookCtx(user_id=None, is_master=True, tier="pro_plus")

    if api_key_user_id == "MISSING":
        raise HTTPException(status_code=401, detail="auth_required")

    user = db.query(User).filter(User.id == api_key_user_id).first()
    if user is None:
        # Authenticated key with no resolvable user row → unauthenticated.
        raise HTTPException(status_code=401, detail="auth_required")

    # evergreen_0206 Phase G: free tier is allowed through (the on-ramp). The
    # tier travels in the ctx so downstream caps/gates enforce per-tier limits.
    tier = user.subscription_tier or "free"
    return CookbookCtx(user_id=user.id, is_master=False, tier=tier)


# ── Schemas ──────────────────────────────────────────────────────────────


class CookbookCreateIn(BaseModel):
    name: str
    description: str | None = None


class SkillAddIn(BaseModel):
    slug: str
    source: str | None = "custom-added"
    # federation_0604 Unit 2 — when set, ``slug`` is the EXTERNAL source's slug
    # and we materialize a private pointer Skill row before linking it. The
    # bundle-provenance ``source`` above (custom-added/forked/…) is unrelated
    # to this federation source id (lobehub/clawhub/skills-sh/…).
    external_source: str | None = None


def _as_slug_list(val: object) -> list[str]:
    """Normalize a skill's related_skills field (jsonb list / JSON string / None) to list[str]."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x]
    if isinstance(val, str):
        import json as _json

        try:
            parsed = _json.loads(val)
            return [str(x) for x in parsed if x] if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


class CookbookSkillOut(BaseModel):
    slug: str
    source: str
    pinned_version: str | None = None
    added_at: datetime | None = None
    # loopclose_3005 Phase E — fields the /bundles/<id> web viz consumes.
    title: str | None = None
    skill_variant: str | None = None  # "catalog" | "custom" (tailored) — badge
    is_public: bool | None = None
    parent_skill_slug: str | None = None  # fork-lineage edge (graph view)
    related_skills: list[str] = []  # declared related-skill edges (graph view)
    pinned: bool = False  # convenience flag: pinned_version is not None
    corrections_absorbed: int = 0  # best-effort field-feedback counter (0 = none)


class CookbookOut(BaseModel):
    id: str
    name: str
    description: str | None = None
    is_base: bool
    parent_bundle_id: str | None = None
    bundle_owner: str | None = None
    created_at: datetime | None = None


# ── Helpers ──────────────────────────────────────────────────────────────


def _resolve_owned_cookbook(db: Session, ctx: CookbookCtx, cookbook_id: str) -> Bundle:
    try:
        cid = UUID(cookbook_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    cb = db.query(Bundle).filter(Bundle.id == cid).first()
    if cb is None:
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    # cookbook_share_2105 Phase D: cbt_token callers (share-token holders) own
    # the resolution path via bundle_scope match. _enforce_cbt_scope_for_cookbook_route  # compat-alias
    # already enforced that ctx.cbt_cookbook_id == cid, so reaching here is
    # authorisation enough.
    if ctx.cbt_cookbook_id is not None and ctx.cbt_cookbook_id == cb.id:
        return cb

    if not ctx.is_master and cb.bundle_owner != ctx.user_id:
        raise HTTPException(status_code=404, detail="cookbook_not_found")
    return cb


def _skills_for(
    db: Session, cookbook_id: UUID, include_disabled: bool = True
) -> list[tuple[BundleSkill, Skill]]:
    q = (
        db.query(BundleSkill, Skill)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == cookbook_id)  # compat-alias
    )
    if not include_disabled:
        q = q.filter(BundleSkill.source != "disabled")
    # portal_0610 J2 — emit in Composer order (install_order), ties by added_at.
    q = q.order_by(BundleSkill.install_order.asc(), BundleSkill.added_at.asc())
    return q.all()


def _corrections_absorbed_count(db: Session, slug: str) -> int:
    """Best-effort count of field-feedback items that referenced this skill.

    loopclose_3005 Phase E. There is no skill_id FK on FeedbackSubmission —
    feedback carries a free-form JSON ``context``. We count rows whose context
    names this slug. Intentionally best-effort: on any error, or when the
    feedback table/columns are absent (older schema), it returns 0 rather than
    raising. The viz treats 0 as "no corrections yet", never a hard claim.
    """
    try:
        from sqlalchemy import String as SAString
        from sqlalchemy import cast

        from app.models import FeedbackSubmission

        # JSON containment is dialect-specific; a portable substring match on the
        # serialized context keeps this working on both Postgres and SQLite tests.
        return (
            db.query(FeedbackSubmission)
            .filter(cast(FeedbackSubmission.context, SAString).contains(slug))
            .count()
        )
    # Rationale: a missing table/column or dialect quirk must not break the
    # bundle view — the counter is decorative, the skill list is load-bearing.
    except Exception:  # noqa: BLE001
        return 0


def _to_cb_out(cb: Bundle) -> dict:
    return CookbookOut(
        id=str(cb.id),
        name=cb.name,
        description=cb.description,
        is_base=bool(cb.is_base),
        parent_bundle_id=str(cb.parent_bundle_id) if cb.parent_bundle_id else None,
        bundle_owner=str(cb.bundle_owner) if cb.bundle_owner else None,
        created_at=cb.created_at,
    ).model_dump(mode="json")


def _cookbook_signals(db: Session, cb: Bundle, skills: list[dict]) -> dict:
    """portal_0610 J6 — living-object signals for a cookbook detail page.

    All honest + organic-only. The cookbook is a living object, not a static
    list: it has reach (installs), a heartbeat (last_synced), team usage (fleet),
    and a feedback rollup. Each signal is best-effort — a query hiccup yields
    null/0 for that field, never a 500 (the skill list is the load-bearing data).

      installs_total / installs_7d : attributed installs (R7 dedup, is_test-excluded)
      last_synced                  : generation token (Cookbook.updated_at)
      fleet_usage                  : how many fleets subscribe this cookbook
      corrections_absorbed         : field-feedback items across member skills
      skill_count                  : active (non-disabled) skills
    """
    from app._skill_helpers import _cookbook_install_counts
    from app.models import FleetSubscription

    signals: dict = {}
    try:
        total, wk = _cookbook_install_counts(db, cb.id)
        signals["installs_total"] = total
        signals["installs_7d"] = wk
    except Exception:  # noqa: BLE001  # Rationale: signal is best-effort observability.
        signals["installs_total"] = None
        signals["installs_7d"] = None
    try:
        updated = getattr(cb, "updated_at", None) or getattr(cb, "created_at", None)
        signals["last_synced"] = updated.isoformat() if updated else None
    except Exception:  # noqa: BLE001  # Rationale: signal is best-effort.
        signals["last_synced"] = None
    try:
        signals["fleet_usage"] = (
            db.query(FleetSubscription).filter(FleetSubscription.bundle_id == cb.id).count()
        )
    except Exception:  # noqa: BLE001  # Rationale: signal is best-effort.
        signals["fleet_usage"] = None
    try:
        active = [s for s in skills if s.get("source") != "disabled"]
        signals["skill_count"] = len(active)
        signals["corrections_absorbed"] = sum(int(s.get("corrections_absorbed") or 0) for s in active)
    except Exception:  # noqa: BLE001  # Rationale: signal is best-effort.
        signals["skill_count"] = None
        signals["corrections_absorbed"] = None
    return signals


# ── spotify_0608 Ph B — public discovery surface ─────────────────────────
# These routes are UNAUTHENTICATED (allowlisted in middleware/api_key.py) and
# MUST be registered before the `/{cookbook_id}` catch-all so FastAPI doesn't
# capture "discover"/"public" as a cookbook_id. They expose ONLY bundles with  # compat-alias
# visibility='public'. Ranking + the public install-count surface EXCLUDE
# test/CI installs via _install_counts_for (§4.2).


def _public_cb_card(db: Session, cb: Bundle) -> dict:
    """A compact, anonymous-safe public cookbook card for the discover feed."""
    skill_rows = _skills_for(db, cb.id, include_disabled=False)
    # portal_0610 R7: count installs ATTRIBUTED TO this bundle (InstallEvent
    # rows stamped with cookbook_id), NOT the sum of each member skill's global
    # install count — the latter double-counts skills shared across bundles.
    from app._skill_helpers import _cookbook_install_counts

    total_installs, installs_7d = _cookbook_install_counts(db, cb.id)
    # portal_0610 R2: emit the owner's CREATOR HANDLE as the ref so the attribution
    # actually validates (a bare owner UUID was dropped by the allowlist). Falls
    # back to the owner UUID string only when no creator/handle exists.
    ref_value = str(cb.bundle_owner) if cb.bundle_owner else None
    if cb.bundle_owner is not None:
        from app.models import Creator

        _creator = db.query(Creator).filter(Creator.user_id == cb.bundle_owner).first()
        if _creator is not None and _creator.handle:
            ref_value = _creator.handle
    return {
        "slug": cb.slug,
        "name": cb.name,
        "description": cb.description,
        "visibility": cb.visibility,
        "theme": cb.theme_json,
        "skill_count": len(skill_rows),
        "installs_total": total_installs,
        "installs_7d": installs_7d,
        "created_at": cb.created_at.isoformat() if cb.created_at else None,
        # spotify_0608 Ph G — verified-maintainer badge on the public card.
        "is_verified": bool(cb.is_verified),
        # ?ref attribution: a creator-tagged clone link surfaced ON the card so
        # install attribution is visible from week 1 (GTM build-plan mod #2).
        # portal_0610 R2: creator HANDLE (validatable), not the raw owner UUID.
        "ref": ref_value,
    }


@_h.get("/discover")
def discover_cookbooks(
    db: Session = Depends(get_db),
    limit: int = 30,
    offset: int = 0,
    sort: str = "installs",
):
    """Public, ranked feed of public cookbooks. No auth required.

    sort: 'installs' (default, by real 7d installs then total — test/CI excluded
    per §4.2) | 'newest' (created_at desc). Pagination via limit/offset.
    """
    limit = max(1, min(int(limit or 30), 100))
    offset = max(0, int(offset or 0))

    q = db.query(Bundle).filter(
        Bundle.visibility == "public",
        Bundle.slug.isnot(None),
    )
    if sort == "newest":
        # portal_0610 R6: add a deterministic tiebreaker. Seeded bundles share
        # one created_at, so without a secondary key the order was arbitrary
        # DB-insertion. Bundle.id.desc() makes "newest" stable + reproducible.
        q = q.order_by(Bundle.created_at.desc(), Bundle.id.desc())
        rows = q.offset(offset).limit(limit).all()
        cards = [_public_cb_card(db, cb) for cb in rows]
    else:
        # Rank by real installs. Small marketplace → compute cards then sort in
        # Python (install counts are a per-bundle aggregate, not a column).
        rows = q.all()
        cards = [_public_cb_card(db, cb) for cb in rows]
        cards.sort(key=lambda c: (c["installs_7d"], c["installs_total"]), reverse=True)
        cards = cards[offset : offset + limit]

    return {"cookbooks": cards, "limit": limit, "offset": offset, "sort": sort}


@_h.get("/public/{slug}")
def public_cookbook_page(slug: str, db: Session = Depends(get_db)):
    """Public cookbook page by slug. No auth. 404 unless visibility='public'.

    Returns the cookbook card + its ordered skill list + a ONE-LINE clone hint
    so an agent can compose it via MCP from the public page (GTM gate, Ph F will
    render this). Carries ?ref attribution.
    """
    cb = db.query(Bundle).filter(Bundle.slug == slug).first()
    if not cb or cb.visibility != "public":
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    card = _public_cb_card(db, cb)
    skill_rows = _skills_for(db, cb.id, include_disabled=False)
    card["skills"] = [
        {
            "slug": skill.slug,
            "title": skill.title,
            "is_public": bool(skill.is_public),
            "source": cs.source,
            "pinned_version": cs.pinned_version,
        }
        for cs, skill in skill_rows
    ]
    # One copy-paste MCP line (the entire top-of-funnel). ?ref makes the install
    # attributable to the creator from the public page.
    ref_q = f"?ref={card['ref']}" if card["ref"] else ""
    card["clone_line"] = f'recipes_cookbook_install from "cookbook://{cb.slug}{ref_q}"'
    return card


# ── spotify_0608 Ph G — reputation surfaces ──────────────────────────────


@_h.get("/leaderboard")
def cookbook_leaderboard(
    db: Session = Depends(get_db),
    limit: int = 10,
):
    """Public reputation leaderboards. No auth.

    Returns two ranked lists over PUBLIC cookbooks:
      - ``top_weekly`` : ranked by REAL 7d installs (then total) — test/CI
        excluded via _install_counts_for (Ph B §4.2). The "top weekly cookbook"
        status surface that gives Day-1 sharers a reason to post again.
      - ``latest``     : most-recently-created public cookbooks ("latest public
        cookbook").
    Each entry is the standard public card (carries is_verified + ?ref).
    """
    limit = max(1, min(int(limit or 10), 50))
    rows = db.query(Bundle).filter(Bundle.visibility == "public", Bundle.slug.isnot(None)).all()
    cards = [_public_cb_card(db, cb) for cb in rows]

    top_weekly = sorted(cards, key=lambda c: (c["installs_7d"], c["installs_total"]), reverse=True)[:limit]

    # latest: created_at desc. Cards don't carry a sortable datetime, so sort the
    # ORM rows then re-card the top N (cheap — leaderboard is small).
    latest_rows = sorted(rows, key=lambda cb: (cb.created_at is not None, cb.created_at), reverse=True)[
        :limit
    ]
    latest = [_public_cb_card(db, cb) for cb in latest_rows]

    return {"top_weekly": top_weekly, "latest": latest, "limit": limit}


@_h.post("/{cookbook_id}/verify")  # compat-alias
def verify_cookbook(
    cookbook_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Assign / revoke the verified-maintainer badge on a cookbook (admin only).

    Master/admin key only — verification is a trust signal we control, not a
    self-serve toggle. Pass ?verified=false to revoke. Returns the new state.
    """
    if not ctx.is_master:
        raise HTTPException(status_code=403, detail="master_key_required")
    try:
        cb_uuid = UUID(cookbook_id)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=404, detail="cookbook_not_found") from exc
    cb = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    if cb is None:
        raise HTTPException(status_code=404, detail="cookbook_not_found")

    verified_q = request.query_params.get("verified", "true").lower()
    cb.is_verified = verified_q not in ("false", "0", "no")
    db.commit()
    return {"cookbook_id": str(cb.id), "is_verified": bool(cb.is_verified)}


# ── Endpoints ────────────────────────────────────────────────────────────


@_h.post("", status_code=201)
def create_cookbook(
    body: CookbookCreateIn,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Create a new cookbook for the authenticated user."""
    if ctx.is_master:
        raise HTTPException(status_code=400, detail="master key cannot create user-owned cookbooks")

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="invalid_name")

    # Cookbook cap — SSOT in config/tiers.yaml via tier_labels.cookbook_limit().
    # None = unlimited (reserved; no current tier). free=1 (evergreen_0206 Phase
    # G on-ramp), Pro=10, Pro+=200.
    limit = cookbook_limit(ctx.tier)
    if limit is not None:
        existing = db.query(Bundle).filter(Bundle.bundle_owner == ctx.user_id).count()  # compat-alias
        if existing >= limit:
            raise HTTPException(
                status_code=403,
                detail={"reason": "pro_tier_limit", "max_cookbooks": limit},
            )

    cb = Bundle(
        id=uuid4(),
        name=name,
        description=body.description,
        is_base=False,
        bundle_owner=ctx.user_id,
    )
    db.add(cb)
    db.commit()
    db.refresh(cb)
    return _to_cb_out(cb)


@_h.get("")
def list_cookbooks(
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """List all cookbooks for the authenticated user."""
    if ctx.is_master:
        return {"cookbooks": []}

    rows = (
        db.query(Bundle)
        .filter(Bundle.bundle_owner == ctx.user_id)  # compat-alias
        .order_by(Bundle.created_at.desc())
        .all()
    )
    return {"cookbooks": [_to_cb_out(r) for r in rows]}


@_h.get("/{cookbook_id}")  # compat-alias
def get_cookbook(
    cookbook_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Return a single cookbook by ID, including its skill list."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)
    rows = _skills_for(db, cb.id, include_disabled=True)
    out = _to_cb_out(cb)
    out["skills"] = [
        CookbookSkillOut(
            slug=skill.slug,
            source=cs.source,
            pinned_version=cs.pinned_version,
            added_at=cs.added_at,
            title=skill.title,
            skill_variant=getattr(skill, "skill_variant", None),
            is_public=bool(skill.is_public),
            parent_skill_slug=getattr(skill, "parent_skill_slug", None),
            related_skills=_as_slug_list(getattr(skill, "related_skills", None)),
            pinned=cs.pinned_version is not None,
            corrections_absorbed=_corrections_absorbed_count(db, skill.slug),
        ).model_dump(mode="json")
        for cs, skill in rows
    ]
    # portal_0610 J6 — living-object signals (the bundle is alive, not a static
    # list). All honest, organic-only counts; the frontend renders what's present.
    out["signals"] = _cookbook_signals(db, cb, out["skills"])
    return out


@_h.post("/{cookbook_id}/skills", status_code=201)  # compat-alias
def add_skill_to_cookbook(
    cookbook_id: str,
    body: SkillAddIn,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Add a skill to the specified cookbook."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)

    source = body.source or "custom-added"
    if source not in ALLOWED_SOURCES:
        raise HTTPException(status_code=422, detail="invalid_source")

    # federation_0604 Unit 2 — external (federated) skill branch.
    # When external_source is set, the body.slug is the external source's slug.
    # Materialize a private pointer Skill row so the FK + all bundle plumbing
    # below work unchanged. Never rehosts: install resolves from origin later.
    if body.external_source:
        from app.services.bundle_external import (
            known_external_source,
            materialize_external_skill,
        )

        if not known_external_source(body.external_source):
            raise HTTPException(status_code=422, detail="unknown_external_source")
        skill = materialize_external_skill(db, body.external_source, body.slug)
        if skill is None:
            raise HTTPException(status_code=404, detail="external_skill_not_found")
    else:
        skill = db.query(Skill).filter(Skill.slug == body.slug).first()
        if skill is None:
            raise HTTPException(status_code=404, detail="skill_not_found")

    existing = (
        db.query(BundleSkill)
        .filter(
            BundleSkill.bundle_id == cb.id,  # compat-alias
            BundleSkill.skill_id == skill.id,
        )
        .first()
    )
    if existing is not None:
        existing.source = source
        _touch_bundle_generation(db, cb.id)
        db.commit()
        return {
            "cookbook_id": str(cb.id),
            "slug": skill.slug,
            "source": existing.source,
            "added_at": existing.added_at.isoformat() if existing.added_at else None,
            "reactivated": True,
            "external": bool(body.external_source),
        }

    # WIS-902: Pro tier skill cap
    if ctx.tier == "pro" or ctx.tier == "cook":  # cook=legacy alias, remove after 2026-06-10
        active_count = (
            db.query(BundleSkill)
            .filter(
                BundleSkill.bundle_id == cb.id,  # compat-alias
                BundleSkill.source != "disabled",
            )
            .count()
        )
        if active_count >= COOK_SKILL_CAP:
            raise HTTPException(
                status_code=403,
                detail={
                    "reason": "pro_skill_cap",
                    "max_skills": COOK_SKILL_CAP,
                    "current_count": active_count,
                    "upgrade_to": "pro_plus",
                },
            )

    cs = BundleSkill(
        bundle_id=cb.id,
        skill_id=skill.id,
        source=source,
    )
    db.add(cs)
    _touch_bundle_generation(db, cb.id)
    db.commit()
    db.refresh(cs)
    return {
        "cookbook_id": str(cb.id),
        "slug": skill.slug,
        "source": cs.source,
        "added_at": cs.added_at.isoformat() if cs.added_at else None,
        "reactivated": False,
        "external": bool(body.external_source),
    }


@_h.delete("/{cookbook_id}/skills/{slug}")  # compat-alias
def remove_skill_from_cookbook(
    cookbook_id: str,
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Remove a skill from the specified cookbook."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)

    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")

    cs = (
        db.query(BundleSkill)
        .filter(
            BundleSkill.bundle_id == cb.id,  # compat-alias
            BundleSkill.skill_id == skill.id,
        )
        .first()
    )
    if cs is None:
        raise HTTPException(status_code=404, detail="skill_not_in_cookbook")

    cs.source = "disabled"
    _touch_bundle_generation(db, cb.id)
    db.commit()
    return {"cookbook_id": str(cb.id), "slug": slug, "source": "disabled", "deleted": True}


# ── portal_0610 J2 — Composer mutations: visibility, version-pin, reorder ────


class VisibilityIn(BaseModel):
    """PATCH body for cookbook visibility (Composer inline toggle, L3)."""

    visibility: str  # 'public' | 'private'


@_h.patch("/{cookbook_id}/visibility")  # compat-alias
def set_cookbook_visibility(
    cookbook_id: str,
    body: VisibilityIn,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """portal_0610 J2 — flip a cookbook public/private from the Composer.

    The Composer surfaces visibility inline (L3). Only the owner (or master) may
    change it; cbt_ share-tokens are scope-gated out by the route guard.
    """
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)
    vis = (body.visibility or "").strip().lower()
    if vis not in {"public", "private"}:
        raise HTTPException(status_code=422, detail="invalid_visibility")
    cb.visibility = vis
    _touch_bundle_generation(db, cb.id)
    db.commit()
    return {"cookbook_id": str(cb.id), "visibility": vis}


class SkillPinIn(BaseModel):
    """PATCH body for a cookbook skill's version pin (L5, curated-only)."""

    pinned_version: str | None = None  # null clears the pin (always-latest)


@_h.patch("/{cookbook_id}/skills/{slug}/pin")  # compat-alias
def set_skill_pin(
    cookbook_id: str,
    slug: str,
    body: SkillPinIn,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """portal_0610 J2 / L5 — pin a curated skill to a specific version, or clear
    the pin (null → always-latest, the default).

    CURATED-ONLY: federation/external skills have no version contract, so pinning
    one is rejected (422). Passing a semver that doesn't exist for the skill 404s
    with the available list (mirrors the install route's pin validation).
    """
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)

    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    cs = (
        db.query(BundleSkill)
        .filter(BundleSkill.bundle_id == cb.id, BundleSkill.skill_id == skill.id)  # compat-alias
        .first()
    )
    if cs is None:
        raise HTTPException(status_code=404, detail="skill_not_in_cookbook")

    # L5: pinning is curated-only. An external/federation skill has no SkillVersion
    # rows and no version contract → cannot be pinned.
    if is_external_skill(skill):
        raise HTTPException(
            status_code=422,
            detail="pin_not_supported_for_external — federation skills install always-latest",
        )

    pin = (body.pinned_version or "").strip() or None
    if pin is not None:
        exists = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == skill.id, SkillVersion.semver == pin)
            .first()
        )
        if exists is None:
            avail = [v.semver for v in db.query(SkillVersion).filter(SkillVersion.skill_id == skill.id).all()]
            raise HTTPException(
                status_code=404,
                detail=f"version '{pin}' not found for '{slug}'. Available: {avail}",
            )
        cs.source = "overridden"  # provenance: explicitly version-pinned
    cs.pinned_version = pin
    _touch_bundle_generation(db, cb.id)
    db.commit()
    return {"cookbook_id": str(cb.id), "slug": slug, "pinned_version": pin, "pinned": pin is not None}


class ReorderIn(BaseModel):
    """PATCH body for Composer reorder — ordered list of skill slugs."""

    order: list[str]  # slugs in the desired install order


@_h.patch("/{cookbook_id}/reorder")  # compat-alias
def reorder_cookbook_skills(
    cookbook_id: str,
    body: ReorderIn,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """portal_0610 J2 / L3 — persist the Composer's skill order.

    Accepts the full ordered list of slugs; assigns install_order = index*10
    (gaps leave room for future single-item moves without a full rewrite). Slugs
    not in the cookbook are ignored; cookbook skills omitted from the list keep
    their existing order after the listed ones (appended by their old order).
    """
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)

    rows = (
        db.query(BundleSkill, Skill)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")  # compat-alias
        .all()
    )
    by_slug = {skill.slug: cs for cs, skill in rows}

    pos = 0
    seen: set[str] = set()
    for slug in body.order:
        cs = by_slug.get(slug)
        if cs is not None:
            cs.install_order = pos * 10
            seen.add(slug)
            pos += 1
    # Anything not named keeps a stable spot AFTER the explicitly-ordered set.
    for slug, cs in by_slug.items():
        if slug not in seen:
            cs.install_order = pos * 10
            pos += 1

    _touch_bundle_generation(db, cb.id)
    db.commit()
    return {
        "cookbook_id": str(cb.id),
        "order": [s for s in body.order if s in by_slug] + [s for s in by_slug if s not in seen],
    }


def _make_install_url(skill_slug: str, version_id: UUID, version_semver: str) -> str:
    """Build a signed download URL for a skill version (Issue #27).

    Uses the same HMAC-signing flow as routes.py:recipes_install so the URL
    resolves to /api/skills/_download?token=<signed> — a route that exists
    and serves the tarball bytes.

    The old implementation pointed at /api/skills/{id}/versions/{id}/tarball
    which has never existed in this codebase (zero route matches).
    """
    from itsdangerous import URLSafeTimedSerializer

    # Issue #27 (secfix_1905/I-followup): salt MUST match install_routes._verify_signed_token.
    # Phase 3+4: primary salt changed to "loopskill-install"; verifier accepts both.
    serializer = URLSafeTimedSerializer(settings.SIGNING_SECRET, salt="loopskill-install")
    token = serializer.dumps({"slug": skill_slug, "version_id": str(version_id), "mode": "install"})
    public_origin = config.public_origin()
    return public_origin.rstrip("/") + "/api/skills/_download?token=" + token


@_h.post("/{cookbook_id}/install")  # compat-alias
def install_cookbook(
    cookbook_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Idempotent: re-running returns the same payload. Disabled skills are skipped."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)
    rows = _skills_for(db, cb.id, include_disabled=False)

    # portal_0610 R1 (§6.6/§6.7-L10): tier-ACCESS gate, owner-tier-scoped.
    # A cbt_ client agent installs against the bundle OWNER's tier, not its
    # own. A free-owner bundle must never emit a Pro tarball; a Pro-owner
    # bundle may. Over-tier skills are SKIPPED in the bulk payload (a mixed
    # bundle still delivers the skills the owner is entitled to) rather than
    # 403-ing the whole install.
    from app.authz import tier_rank_allows_install
    from app._skill_helpers import _resolve_cookbook_owner_tier

    owner_tier = _resolve_cookbook_owner_tier(db, cb)

    skills_payload = []
    installed_skills: list[tuple[Skill, str, int]] = []
    for cs, skill in rows:
        # portal_0610 R1: skip skills the bundle owner's tier cannot install.
        if not tier_rank_allows_install(owner_tier, getattr(skill, "tier", None)):
            continue
        # federation_0604 Unit 2 — external rows get a CHEAP descriptor + a
        # bundle-scoped single-install URL. No origin fetch in the bulk path
        # (isolation wall #2: bulk must not fan out N network calls).
        if is_external_skill(skill):
            skills_payload.append(install_descriptor_for(str(cb.id), skill))
            continue
        version = None
        if cs.pinned_version:
            version = (
                db.query(SkillVersion)
                .filter(
                    SkillVersion.skill_id == skill.id,
                    SkillVersion.semver == cs.pinned_version,
                )
                .first()
            )
        if version is None:
            version = (
                db.query(SkillVersion)
                .filter(SkillVersion.skill_id == skill.id)
                .order_by(SkillVersion.created_at.desc())
                .first()
            )

        entry = {
            "slug": skill.slug,
            "version": version.semver if version else None,
            "tarball_url": _make_install_url(skill.slug, version.id, version.semver) if version else None,
            "checksum_sha256": version.checksum_sha256 if version else None,
            "source": cs.source,
        }
        skills_payload.append(entry)
        if version is not None:
            # Track the payload index so we can stamp this entry's provenance_id
            # PER-SKILL after recording (R4 nit (a): provenance rides per-skill
            # under skills[], NOT bundle-top-level).
            installed_skills.append((skill, version.semver, len(skills_payload) - 1))

    # spotify_0608 Ph E — record an InstallEvent + mint a PER-SKILL provenance_id
    # for every skill that returned a real version, stamping cookbook_id so the
    # feedback harness can route a later report to THIS bundle's curator repo.
    # (Supersedes recipes-D's _record_install_event: same counter + is_test
    # integrity via record_install_with_provenance, plus provenance.)
    from app.services.provenance import record_install_with_provenance

    for skill, semver, idx in installed_skills:
        _ev, provenance_id = record_install_with_provenance(
            db,
            skill=skill,
            version_semver=semver,
            request=request,
            source="cookbook",
            cookbook_id=cb.id,
        )
        skills_payload[idx]["provenance_id"] = provenance_id
    if installed_skills:
        db.commit()

    return {
        "cookbook_id": str(cb.id),
        "name": cb.name,
        "skills": skills_payload,
    }


@_h.get("/{cookbook_id}/manifest")  # compat-alias
def cookbook_manifest(
    cookbook_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Return the install manifest for all skills in a cookbook."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)
    rows = _skills_for(db, cb.id, include_disabled=True)

    manifest = {
        "name": cb.name,
        "description": cb.description,
        "skills": [
            {
                "slug": skill.slug,
                "source": cs.source,
                "pinned_version": cs.pinned_version,
            }
            for cs, skill in rows
        ],
    }
    body = yaml.safe_dump(manifest, sort_keys=False, default_flow_style=False)
    return Response(content=body, media_type="application/x-yaml")


@_h.get("/{cookbook_id}/sync")  # compat-alias
def cookbook_sync(
    cookbook_id: str,
    request: Request,
    since: str | None = None,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Return skills updated since the given timestamp for sync."""
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid_since")
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=UTC)

    q = (
        db.query(BundleSkill, Skill)
        .join(Skill, Skill.id == BundleSkill.skill_id)
        .filter(BundleSkill.bundle_id == cb.id)  # compat-alias
    )
    if since_dt is not None:
        # SQLite stores naive datetimes; compare naively if necessary.
        q = q.filter(BundleSkill.added_at >= since_dt.replace(tzinfo=None))

    added: list[dict] = []
    removed: list[dict] = []
    updated: list[dict] = []
    for cs, skill in q.all():
        evt = {
            "slug": skill.slug,
            "source": cs.source,
            "pinned_version": cs.pinned_version,
            "added_at": cs.added_at.isoformat() if cs.added_at else None,
        }
        if cs.source == "disabled":
            removed.append(evt)
        elif cs.source == "overridden":
            updated.append(evt)
        else:
            added.append(evt)

    return {
        "cookbook_id": str(cb.id),
        "since": since_dt.isoformat() if since_dt else None,
        "added": added,
        "removed": removed,
        "updated": updated,
    }


# ── cookbook_share_2105 Phase D — single-skill install under bundle prefix ──  # compat-alias


@_h.get("/{cookbook_id}/skills/{slug}/install")  # compat-alias
def install_single_skill_from_cookbook(
    cookbook_id: str,
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Install ONE skill from a cookbook by slug.

    Mirror of ``GET /api/skills/install`` but scoped under a cookbook so cbt_
    share tokens (which can ONLY access /api/cookbooks/* paths — see
    middleware.py:389) have a documented single-skill install path.

    Behaviour:
    - 200 with {slug, version, tarball_url, checksum_sha256} on success
    - 404 if the skill is not in this cookbook (or doesn't exist)
    - 403 if the cbt_ token's scope is 'read' (install IS a write-flavoured
      action even though it's GET — gated identically to POST /install)

    Token scope rules (enforced in _enforce_cbt_scope_for_cookbook_route):
      read    → 403 SCOPE_INSUFFICIENT
      install → ok
      edit    → ok (superset of install)
      master/user (owner) → ok
    """
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)

    # install is a write-flavoured action on GET — even with read scope this
    # should 403. The scope-gate above passes 'read' through for any GET, so
    # add a dedicated install-route block here.
    if getattr(request.state, "cookbook_token_scope", None) == "read":
        raise HTTPException(
            status_code=403,
            detail="SCOPE_INSUFFICIENT: token scope 'read' insufficient; need 'install' or higher",
        )

    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)

    # Find the skill globally; then check it's actually in this bundle
    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if skill is None:
        raise HTTPException(status_code=404, detail="skill_not_found")

    cs = (
        db.query(BundleSkill)
        .filter(
            BundleSkill.bundle_id == cb.id,  # compat-alias
            BundleSkill.skill_id == skill.id,
            BundleSkill.source != "disabled",
        )
        .first()
    )
    if cs is None:
        raise HTTPException(status_code=404, detail="skill_not_in_cookbook")

    # portal_0610 R1 (§6.6/§6.7-L10): tier-ACCESS gate, owner-tier-scoped.
    # An explicit single-skill install of an over-tier skill 403s (unlike the
    # bulk path which silently skips). The bundle OWNER's tier governs, so a
    # free-owner bundle cannot hand a client agent a Pro skill even by direct
    # slug. External skills carry no tarball/tier contract → not gated here.
    if not is_external_skill(skill):
        from app.authz import tier_rank_allows_install
        from app._skill_helpers import _resolve_cookbook_owner_tier

        owner_tier = _resolve_cookbook_owner_tier(db, cb)
        if not tier_rank_allows_install(owner_tier, getattr(skill, "tier", None)):
            from app.tier_labels import display_label as _dl

            raise HTTPException(
                status_code=403,
                detail=(
                    f"This skill requires {_dl(skill.tier or 'pro')} tier; the "
                    f"cookbook owner's plan does not include it."
                ),
            )

    # federation_0604 Unit 2 — external skill install resolves the real SKILL.md
    # from origin at install time (never rehosted), via the SHARED resolver also
    # used by /api/skills/external/.../install. No SkillVersion/tarball exists.
    if is_external_skill(skill):
        from app.services.bundle_external import descriptor_source_slug

        src_slug = descriptor_source_slug(skill)
        if src_slug is None:
            raise HTTPException(status_code=404, detail="external_descriptor_missing")
        source, ext_slug = src_slug
        payload = resolve_external_install(source, ext_slug)
        if payload is None:
            raise HTTPException(
                status_code=404,
                detail="external_skill_unresolvable",
            )
        # spotify_0608 Ph E — external single-install fetched the real body, so
        # this IS an attributed install (we know source+slug+bundle). Record +
        # mint provenance. (A deep-link/non-fetch external skill never resolves a
        # body → resolve_external_install returns None → 404 above, never here;
        # the honestly-unattributed path is for the federated catalogue where the
        # router declines a body — see skill_routes public external install.)
        from app.services.provenance import record_install_with_provenance

        _ev, provenance_id = record_install_with_provenance(
            db,
            skill=skill,
            version_semver="external",
            request=request,
            source="cookbook",
            cookbook_id=cb.id,
        )
        db.commit()
        return {**payload, "external": True, "source": cs.source, "provenance_id": provenance_id}

    # Pick the right version: pinned if set, else latest
    version: SkillVersion | None = None
    if cs.pinned_version:
        version = (
            db.query(SkillVersion)
            .filter(
                SkillVersion.skill_id == skill.id,
                SkillVersion.semver == cs.pinned_version,
            )
            .first()
        )
    if version is None:
        version = (
            db.query(SkillVersion)
            .filter(SkillVersion.skill_id == skill.id)
            .order_by(SkillVersion.created_at.desc())
            .first()
        )

    if version is None:
        raise HTTPException(status_code=404, detail="no_versions")

    # spotify_0608 Ph E — record install + mint provenance (stamps cookbook_id
    # so a later feedback/skill-error report routes to THIS bundle's curator).
    from app.services.provenance import record_install_with_provenance

    _ev, provenance_id = record_install_with_provenance(
        db,
        skill=skill,
        version_semver=version.semver,
        request=request,
        source="cookbook",
        cookbook_id=cb.id,
    )
    db.commit()

    return {
        "slug": skill.slug,
        "version": version.semver,
        "tarball_url": _make_install_url(skill.slug, version.id, version.semver),
        "checksum_sha256": version.checksum_sha256,
        "source": cs.source,
        "provenance_id": provenance_id,
    }


# ── loopclose_3005 Phase I — bundle handoff REST endpoint ──────────────


class HandoffIn(BaseModel):
    """Request body for POST /api/cookbooks/{id}/handoff."""

    new_owner_user_id: str | None = None
    new_owner_email: str | None = None
    mode: str = "transfer"


@_h.post("/{cookbook_id}/handoff")  # compat-alias
def handoff_cookbook(
    cookbook_id: str,
    body: HandoffIn,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """Transfer or fork a cookbook to a new owner.

    Only the current cookbook owner (or master) may call this endpoint.
    Delegates to the MCP tool implementation for a single source of truth.
    """
    from app.auth_ctx import AuthContext
    from app.mcp.tools.bundle_handoff import recipes_cookbook_handoff  # compat-alias

    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)

    # Build an AuthContext from the CookbookCtx for the tool
    if ctx.is_master:
        auth_ctx: AuthContext = AuthContext(scope="master")
    elif ctx.user_id is not None:
        auth_ctx = AuthContext(scope="user", user_id=ctx.user_id)
    else:
        raise HTTPException(status_code=401, detail="auth_required")

    result = recipes_cookbook_handoff(
        db,
        ctx=auth_ctx,
        cookbook_id=cookbook_id,
        new_owner_user_id=body.new_owner_user_id,
        new_owner_email=body.new_owner_email,
        mode=body.mode,
    )

    if "error" in result:
        error = result["error"]
        if error == "cookbook_not_found":
            raise HTTPException(status_code=404, detail=error)
        if error == "forbidden":
            raise HTTPException(status_code=403, detail=error)
        if error == "new_owner_not_found":
            raise HTTPException(status_code=404, detail=error)
        raise HTTPException(status_code=400, detail=error)

    return result


# ── portal_0610 J8 — feedback-repo binding (delivery cockpit) ────────────────


class FeedbackConfigIn(BaseModel):
    """PATCH body for cookbook feedback routing (J8 cockpit binding UI)."""

    repo: str | None = None  # 'owner/name'; null clears → default routing
    mode: str | None = "pat"
    pat: str | None = None  # fine-grained GitHub PAT (issues:write); never stored plaintext


@_h.get("/{cookbook_id}/feedback-config")  # compat-alias
def get_feedback_config(
    cookbook_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """J8 — read where this cookbook's feedback routes (for the inbox panel).

    Never returns the PAT — only repo + mode + whether a credential is bound.
    """
    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    cb = _resolve_owned_cookbook(db, ctx, cookbook_id)
    return {
        "cookbook_id": str(cb.id),
        "feedback_repo": cb.feedback_repo,
        "feedback_mode": cb.feedback_mode,
        "has_credential": bool(cb.feedback_pat_enc),
        "default_repo": "wisechef-ai/recipes-api",
    }


@_h.patch("/{cookbook_id}/feedback-config")  # compat-alias
def set_feedback_config(
    cookbook_id: str,
    body: FeedbackConfigIn,
    request: Request,
    db: Session = Depends(get_db),
    ctx: CookbookCtx = Depends(require_cookbook_tier),
):
    """J8 — bind (or clear) per-cookbook feedback routing from the cockpit.

    Delegates to the MCP tool for a single source of truth (tier gate, ownership,
    repo validation, PAT verification + encryption all live there). Pro/Pro+ only.
    """
    from app.auth_ctx import AuthContext
    from app.mcp.tools.configure_feedback import recipes_configure_feedback

    _enforce_cbt_scope_for_cookbook_route(request, cookbook_id)
    # Resolve ownership first (404/403 before the tool's softer error dict).
    _resolve_owned_cookbook(db, ctx, cookbook_id)

    if ctx.is_master:
        auth_ctx: AuthContext = AuthContext(scope="master")
    elif ctx.user_id is not None:
        auth_ctx = AuthContext(scope="user", user_id=ctx.user_id, tier=ctx.tier)
    else:
        raise HTTPException(status_code=401, detail="auth_required")

    result = recipes_configure_feedback(
        db,
        ctx=auth_ctx,
        cookbook_id=cookbook_id,
        repo=body.repo,
        mode=body.mode,
        pat=body.pat,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result.get("error", "feedback_config_failed"))
    return result


# ── Phase 3+4: combined router with new canonical prefix + compat alias ──────
# /api/bundles is the new primary vocabulary; /api/cookbooks is kept as a  # compat-alias
# compat-alias so existing clients, agents, and integrations continue to work.
router = APIRouter()
router.include_router(_h, prefix="/api/bundles", tags=["bundles"])
router.include_router(_h, prefix="/api/cookbooks", tags=["cookbooks"])  # compat-alias
