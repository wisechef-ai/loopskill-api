"""Demand Brief — master-key-gated content-marketing direction feed.

`demandbrief_3005`: Recipes markets via content only (no cold-outreach), and the
single biggest gap is *search demand we are not serving*. This module turns the
data we already collect into a scored, content-actionable brief that Chef's
content factory reads at runtime to decide WHAT to produce next — flipping
marketing from push (our hand-written topic bank) to pull (what people typed
into the search box and what is organically bundling itself).

Why ``/api/admin`` and NOT ``/api/marketing``
---------------------------------------------
``/api/marketing/`` is an UNAUTHENTICATED public prefix (middleware allowlist) so
the static-build pipeline can pull catalog counts. The demand brief exposes our
strategic gaps — zero-result searches, dead distribution funnels, organic
co-install clusters a competitor could copy. It MUST stay behind the master key.
Chef fetches it server-side with the master key; it never touches a public surface.

Signals merged (all FIRST-PARTY — already collected, zero new instrumentation)
------------------------------------------------------------------------------
1. ``MissingSkillQuery``  — searches that returned zero results. The purest
   demand signal that exists: someone declared intent, we had nothing. Drives
   the "We built what you searched for" content angle.
2. ``SkillDerivedEdge``   — organic co-install clusters (the agency bundle
   assembling itself: cold-outreach ↔ proposal-builder ↔ seo-audit ↔
   whitelabel-dashboard). Drives the "You can SEND this as a cookbook" angle —
   the distribution-activation MRR unlock.
3. ``InstallEvent`` + ``Cookbook`` + ``Fleet`` — the distribution funnel. When
   cookbooks/fleets sit at ~zero adoption while skills install fine, the gap is
   marketing education, not catalog. Surfaced as an explicit activation theme.

Each theme carries a ``score`` so Chef can pick the highest-leverage one, and a
``content_angle`` + ``evidence`` so the post it produces is grounded in a real
number (honesty gate — no fabricated proof).

Scoring (search-demand-led, per Adam 2026-05-30: "search demand is the gap")
----------------------------------------------------------------------------
    score = 0.35 * search_demand      # how many people asked (MissingSkillQuery count)
          + 0.30 * mrr_leverage       # pushes cookbook / fleet / paid tier?
          + 0.20 * content_readiness  # can we ship an HONEST post today?
          + 0.15 * wtp_proof          # external willingness-to-pay (0 here; layer-2 fills it)

Layer-2 (Reddit/X/competitor mining + external WTP) writes into ``wtp_proof`` via
a future ``POST /api/admin/demand-themes`` + ``demand_themes`` table. This module
ships the first-party foundation; it degrades gracefully (every block is wrapped)
so a missing/empty table never 500s the brief.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    Cookbook,
    Fleet,
    InstallEvent,
    MissingSkillQuery,
    Skill,
    SkillDerivedEdge,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ── Scoring weights (search-demand-led) ──────────────────────────────────────
W_SEARCH_DEMAND = 0.35
W_MRR_LEVERAGE = 0.30
W_CONTENT_READINESS = 0.20
W_WTP_PROOF = 0.15

# Self-install / fleet-churn slugs that contaminate the install signal. These are
# our own infra installing on our own agents, not external demand. Excluded from
# "real traction" math so the brief never markets a vanity number.
# (super-memory = Tori's own memory-stack installer; ~79% of lifetime installs.)
_VANITY_SLUGS = frozenset({"super-memory"})


def _norm(value: float, ceiling: float) -> float:
    """Clamp value/ceiling into [0,1]. ceiling<=0 → 0.0 (defensive)."""
    if ceiling <= 0:
        return 0.0
    return min(1.0, max(0.0, value / ceiling))


def _zero_result_themes(db: Session, days: int, limit: int) -> list[dict]:
    """Top zero-result search queries → 'we-built-what-you-searched-for' themes.

    The highest-converting content that exists: it markets to people who already
    declared intent by typing the query and finding nothing.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).date()
    rows = (
        db.query(
            MissingSkillQuery.query.label("q"),
            func.sum(MissingSkillQuery.count).label("hits"),
        )
        .filter(MissingSkillQuery.day >= since)
        .group_by(MissingSkillQuery.query)
        .order_by(func.sum(MissingSkillQuery.count).desc())
        .limit(limit)
        .all()
    )
    if not rows:
        return []

    top_hits = int(rows[0].hits or 1)
    themes: list[dict] = []
    for r in rows:
        hits = int(r.hits or 0)
        search_demand = _norm(hits, top_hits)
        # A zero-result query is, by definition, something we do NOT have a skill
        # for yet — so content_readiness is LOW (we'd be marketing a gap, not a
        # product). The honest play is "build it, then post 'you asked, it's
        # live'" — so we flag build_first=True rather than inflate readiness.
        themes.append(
            {
                "kind": "search_gap",
                "label": str(r.q),
                "evidence": f"{hits} zero-result searches in last {days}d",
                "content_angle": (
                    f'"You asked for {r.q!s} — it is live." '
                    "Ship the skill first, then post the reveal to people who already searched."
                ),
                "build_first": True,
                "_components": {
                    "search_demand": round(search_demand, 3),
                    "mrr_leverage": 0.4,  # a served gap converts the intent-declared
                    "content_readiness": 0.2,  # low until the skill exists
                    "wtp_proof": 0.0,
                },
            }
        )
    return themes


def _coinstall_cluster_themes(db: Session, limit: int) -> list[dict]:
    """Organic co-install clusters → 'send-this-as-a-cookbook' (MRR) themes.

    When skills co-install, the market is asking for the *bundle*, delivered and
    sendable as one cookbook. This is the distribution-activation angle — the
    multi-seat / recurring-revenue motion, weighted highest on MRR leverage.
    """
    rows = (
        db.query(
            SkillDerivedEdge.source_slug,
            SkillDerivedEdge.target_slug,
            SkillDerivedEdge.weight,
        )
        .order_by(SkillDerivedEdge.weight.desc())
        .limit(200)
        .all()
    )
    if not rows:
        return []

    # Dedupe to undirected pairs, keep the strongest.
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str, float]] = []
    for src, tgt, w in rows:
        key = tuple(sorted([src, tgt]))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((key[0], key[1], float(w)))
        if len(pairs) >= limit:
            break

    if not pairs:
        return []

    top_w = pairs[0][2] or 1.0
    themes: list[dict] = []
    for a, b, w in pairs:
        strength = _norm(w, top_w)
        themes.append(
            {
                "kind": "cookbook_bundle",
                "label": f"{a} + {b}",
                "evidence": f"co-install weight {w:.2f} (organic, derived-edge)",
                "content_angle": (
                    f"These install together. Bundle {a} + {b} into one cookbook, "
                    "send the install link to your team or client — they install in one "
                    "command, you keep the margin. (Pro+ cookbook-send = the MRR motion.)"
                ),
                "build_first": False,  # skills already exist — it's a packaging post
                "_components": {
                    # Organic co-install strength IS the implied-demand signal here.
                    "search_demand": round(strength, 3),
                    "mrr_leverage": 0.9,  # cookbook-send is the paid recurring motion
                    "content_readiness": 0.9,  # both skills live → honest post today
                    "wtp_proof": 0.0,
                },
            }
        )
    return themes


def _activation_gap_theme(db: Session, days: int) -> dict | None:
    """Distribution-activation funnel → 'stop hand-wiring, start syncing' theme.

    If skills install fine but cookbooks/fleets sit near zero, the gap is
    marketing education, not catalog. This is a standing theme until the
    distribution funnel shows real external adoption.
    """
    since = datetime.now(UTC) - timedelta(days=days)

    installs_7d = db.query(func.count(InstallEvent.id)).filter(InstallEvent.created_at >= since).scalar() or 0
    # De-vanitize: strip self-install / fleet-churn slugs.
    vanity_7d = (
        db.query(func.count(InstallEvent.id))
        .filter(
            InstallEvent.created_at >= since,
            InstallEvent.skill_slug.in_(_VANITY_SLUGS),
        )
        .scalar()
        or 0
    )
    real_installs_7d = max(0, int(installs_7d) - int(vanity_7d))

    # Personal/forked cookbooks = real adoption (base cookbook is ours).
    cookbooks_adopted = (
        db.query(func.count(Cookbook.id))
        .filter(Cookbook.is_base == False)  # noqa: E712
        .scalar()
        or 0
    )
    fleets_total = db.query(func.count(Fleet.id)).scalar() or 0

    # Only surface the theme when the funnel is actually under-activated:
    # real skill installs exist but the distribution layer is empty.
    if real_installs_7d <= 0 and cookbooks_adopted == 0 and fleets_total == 0:
        # Nothing installs at all — activation isn't the story; demand is.
        return None
    if cookbooks_adopted > 0 and fleets_total > 0:
        # Distribution layer has traction — theme no longer the priority.
        return None

    return {
        "kind": "distribution_activation",
        "label": "Activate cookbook-send & fleet-sync",
        "evidence": (
            f"{real_installs_7d} real skill installs/7d (ex-vanity) but "
            f"{cookbooks_adopted} adopted cookbooks / {fleets_total} fleets"
        ),
        "content_angle": (
            "People install single skills but nobody bundles + sends them. Educate the "
            "cookbook-send motion: 'They give you skills. We give you a registry you push "
            "to 10 agents at once.' This is the recurring-revenue unlock — market it hard."
        ),
        "build_first": False,  # feature is shipped; this is pure education content
        "_components": {
            "search_demand": 0.5,  # standing strategic priority
            "mrr_leverage": 1.0,  # the single highest-MRR motion on the platform
            "content_readiness": 0.85,  # feature live → honest post today
            "wtp_proof": 0.0,
        },
    }


def _score(components: dict) -> float:
    return round(
        W_SEARCH_DEMAND * components.get("search_demand", 0.0)
        + W_MRR_LEVERAGE * components.get("mrr_leverage", 0.0)
        + W_CONTENT_READINESS * components.get("content_readiness", 0.0)
        + W_WTP_PROOF * components.get("wtp_proof", 0.0),
        4,
    )


def build_demand_brief(db: Session, days: int = 14, limit: int = 8) -> dict:
    """Assemble the scored, content-actionable demand brief.

    Pure function (no Request / auth) so it is unit-testable and reusable by a
    future MCP tool or cron without an HTTP round-trip. Every signal block is
    independently guarded — one empty/broken table degrades that block to [],
    never 500s the whole brief.
    """
    themes: list[dict] = []

    for builder, args in (
        (_zero_result_themes, (db, days, limit)),
        (_coinstall_cluster_themes, (db, limit)),
    ):
        try:
            themes.extend(builder(*args))
        # Rationale: a missing/empty signal table must degrade to no-themes, not 500.
        except Exception:  # noqa: BLE001
            logger.exception("demand-brief block failed: %s", builder.__name__)

    try:
        activation = _activation_gap_theme(db, days)
        if activation:
            themes.append(activation)
    # Rationale: funnel query is best-effort; never let it break the brief.
    except Exception:  # noqa: BLE001
        logger.exception("demand-brief activation block failed")

    # Score + rank. Lift components to top-level, drop the private bag.
    for t in themes:
        comps = t.pop("_components", {})
        t["score"] = _score(comps)
        t["score_components"] = comps
    themes.sort(key=lambda t: t["score"], reverse=True)

    total_public_skills = (
        db.query(func.count(Skill.id))
        .filter(Skill.is_public == True, Skill.is_archived == False)  # noqa: E712
        .scalar()
        or 0
    )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "window_days": days,
        "weights": {
            "search_demand": W_SEARCH_DEMAND,
            "mrr_leverage": W_MRR_LEVERAGE,
            "content_readiness": W_CONTENT_READINESS,
            "wtp_proof": W_WTP_PROOF,
        },
        "top_theme": themes[0] if themes else None,
        "themes": themes,
        "catalog": {"public_skills": int(total_public_skills)},
        "_note": (
            "First-party demand signals only. Layer-2 (external Reddit/X/competitor "
            "WTP) fills score_components.wtp_proof via POST /api/admin/demand-themes "
            "(future). Chef reads top_theme to pick what content to produce next."
        ),
    }


@router.get("/demand-brief")
def demand_brief(
    request: Request,
    days: int = Query(14, ge=1, le=90, description="Look-back window for search + funnel signals"),
    limit: int = Query(8, ge=1, le=25, description="Max themes per signal block"),
    db: Session = Depends(get_db),
) -> dict:
    """Master-key-gated content-marketing direction feed for Chef's content factory.

    Master-key only (``api_key_user_id is None``) — the brief exposes strategic
    demand gaps that must never hit a public surface. Chef fetches server-side.
    """
    api_key_user_id = getattr(request.state, "api_key_user_id", "MISSING")
    if api_key_user_id is not None:
        raise HTTPException(status_code=403, detail="Admin only")
    return build_demand_brief(db, days=days, limit=limit)


def _cli() -> int:
    """Local JSON emitter for the demand brief — no HTTP, no auth.

    Run ON the host that owns the DB (wisechef-hq):

        python -m app.demand_routes --json [--days N] [--limit M]

    This is the producer path for demandbrief_3005 P1: a Tori bridge cron SSHes
    into wisechef-hq, runs this against the LOCAL DB session, captures stdout
    JSON, and renders it into the Obsidian shared-knowledge vault. No admin key
    is ever provisioned anywhere — the brief is computed where the data lives.
    """
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="Emit the Recipes demand brief as JSON.")
    parser.add_argument("--json", action="store_true", help="Emit JSON to stdout (default).")
    parser.add_argument("--days", type=int, default=14, help="Look-back window (1-90).")
    parser.add_argument("--limit", type=int, default=8, help="Max themes per signal block (1-25).")
    args = parser.parse_args()

    days = max(1, min(90, args.days))
    limit = max(1, min(25, args.limit))

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        brief = build_demand_brief(db, days=days, limit=limit)
    finally:
        db.close()

    print(_json.dumps(brief, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
