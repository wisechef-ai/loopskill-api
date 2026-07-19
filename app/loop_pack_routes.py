"""Loop packs — curated, named groupings of proven verifiers/loops.

REVENUE/CATALOG (atomic-habits fallback, 2026-07-18). The browse page
(``/api/loops``) surfaces 10 loose loops with no curation signal. This route
groups the registry's two battle-tested loops (real run_count >= 1) into one
named "Fleet Ops" pack so the browse UI has a curated top shelf to point at,
raising install AOV without touching pricing/Stripe.

Deliberately NO new DB table. A full "bundle of verifiers" primitive (a
BundleVerifier join model, mirroring BundleSkill/BundleCompositeLoop) is a
real schema addition — migration + authz + tests — and does not fit a
10-minute fallback-ship window. This route reads the pack membership from a
small in-module constant and resolves it LIVE against the Verifier table, so
if a member loop is renamed/retired the pack degrades honestly (drops the
missing member, never fabricates data) instead of drifting stale.

Routes:
  GET /api/loops/packs                — list curated packs (currently: fleet-ops)
  GET /api/loops/packs/{pack_slug}     — pack detail with live-resolved members
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session
from fastapi import Depends

from app.database import get_db
from app.models import Verifier

router = APIRouter(tags=["loop-packs"])

# Curated pack definitions. member_slugs point at Verifier.slug rows.
# Add a new pack here when a fresh cohort of loops earns real run_count.
LOOP_PACKS: dict[str, dict[str, str | list[str]]] = {
    "fleet-ops": {
        "title": "Fleet Ops",
        "description": (
            "The two battle-tested ops loops in the registry: secret-scan-loop "
            "and repo-steward-loop. Both have real run history — this pack is "
            "the curated top shelf, not the loose 10-loop browse list."
        ),
        "member_slugs": ["secret-scan-loop", "repo-steward-loop"],
    },
}


def _resolve_pack(pack_slug: str, db: Session) -> dict:
    pack = LOOP_PACKS.get(pack_slug)
    if pack is None:
        raise HTTPException(status_code=404, detail=f"loop pack {pack_slug!r} not found")

    member_slugs = pack["member_slugs"]
    assert isinstance(member_slugs, list)
    rows = db.query(Verifier).filter(Verifier.slug.in_(member_slugs), Verifier.is_archived.is_(False)).all()
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
