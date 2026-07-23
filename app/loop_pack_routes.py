"""Loop packs — curated, named groupings of proven verifiers/loops + composite loops.

REVENUE/CATALOG (atomic-habits fallback, 2026-07-18). The browse page
(``/api/loops``) surfaces 10 loose loops with no curation signal. This route
groups the registry's two battle-tested loops (real run_count >= 1) into one
named "Fleet Ops" pack so the browse UI has a curated top shelf to point at,
raising install AOV without touching pricing/Stripe.

REVENUE/CATALOG (atomic-habits fallback, 2026-07-22). Added a "self-improvement"
pack that bundles the two flagship composite loops (atomic-habits + dreaming).
These are v1.0.0, is_public, install_count 0 — undiscoverable via any curated
surface. The pack gives them a top-shelf home alongside Fleet Ops.

Deliberately NO new DB table. A full "bundle of verifiers" primitive (a
BundleVerifier join model, mirroring BundleSkill/BundleCompositeLoop) is a
real schema addition — migration + authz + tests — and does not fit a
10-minute fallback-ship window. This route reads the pack membership from a
small in-module constant and resolves it LIVE against the Verifier OR
CompositeLoop table (based on member_kind), so if a member is renamed/retired
the pack degrades honestly (drops the missing member, never fabricates data)
instead of drifting stale.

Routes:
  GET /api/loops/packs                — list curated packs
  GET /api/loops/packs/{pack_slug}     — pack detail with live-resolved members
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session
from fastapi import Depends

from app.database import get_db
from app.models import Verifier, CompositeLoop

router = APIRouter(tags=["loop-packs"])

# Curated pack definitions.
# - member_kind "verifier" resolves against the Verifier (loops) table.
# - member_kind "composite_loop" resolves against the CompositeLoop table.
# Add a new pack here when a fresh cohort earns its place on the top shelf.
LOOP_PACKS: dict[str, dict] = {
    "fleet-ops": {
        "title": "Fleet Ops",
        "description": (
            "The two battle-tested ops loops in the registry: secret-scan-loop "
            "and repo-steward-loop. Both have real run history — this pack is "
            "the curated top shelf, not the loose 10-loop browse list."
        ),
        "member_kind": "verifier",
        "member_slugs": ["secret-scan-loop", "repo-steward-loop"],
    },
    "self-improvement": {
        "title": "Self-Improvement",
        "description": (
            "Two flagship composite loops that make your agent better every day: "
            "atomic-habits (ship one 1% improvement nightly) and dreaming (memory "
            "consolidation during low-activity hours). Both are deployable "
            "autonomous work units with a verifier quality gate."
        ),
        "member_kind": "composite_loop",
        "member_slugs": ["atomic-habits", "dreaming"],
    },
}


def _resolve_pack(pack_slug: str, db: Session) -> dict:
    pack = LOOP_PACKS.get(pack_slug)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"loop pack {pack_slug!r} not found")

    member_slugs = pack["member_slugs"]
    assert isinstance(member_slugs, list)
    kind = pack.get("member_kind", "verifier")

    if kind == "composite_loop":
        rows = (
            db.query(CompositeLoop)
            .filter(CompositeLoop.slug.in_(member_slugs), CompositeLoop.is_archived.is_(False))
            .all()
        )
        by_slug = {c.slug: c for c in rows}
        members = []
        missing = []
        for slug in member_slugs:
            c = by_slug.get(slug)
            if c is None:
                missing.append(slug)
                continue
            members.append(
                {
                    "slug": c.slug,
                    "title": c.title,
                    "run_count": 0,  # composite loops don't have run_count; use install_count
                    "install_count": c.install_count or 0,
                    "category": c.tier or "free",
                    "tags": list(c.tags or []),
                }
            )
    else:
        rows = (
            db.query(Verifier)
            .filter(Verifier.slug.in_(member_slugs), Verifier.is_archived.is_(False))
            .all()
        )
        by_slug = {v.slug: v for v in rows}
        members = []
        missing = []
        for slug in member_slugs:
            v = by_slug.get(slug)
            if v is None:
                missing.append(slug)
                continue
            members.append(
                {
                    "slug": v.slug,
                    "title": v.title,
                    "run_count": v.run_count or 0,
                    "install_count": v.install_count or 0,
                    "category": v.category,
                }
            )

    return {
        "pack_slug": pack_slug,
        "title": pack["title"],
        "description": pack["description"],
        "members": members,
        "member_count": len(members),
        "missing_members": missing,  # honest signal if a curated member drifted away
    }


def list_loop_packs(db: Session = Depends(get_db)) -> list[dict]:
    """List curated loop packs (summary — no member resolution)."""
    return [{"pack_slug": slug, "title": p["title"]} for slug, p in LOOP_PACKS.items()]


def get_loop_pack(pack_slug: str, db: Session = Depends(get_db)) -> dict:
    """Curated loop pack detail with live-resolved members."""
    return _resolve_pack(pack_slug, db)


router.add_api_route("/api/loops/packs", list_loop_packs, methods=["GET"])
router.add_api_route("/api/loops/packs/{pack_slug}", get_loop_pack, methods=["GET"])
