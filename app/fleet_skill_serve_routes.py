"""fleetos_1607 Phase T — the trojan skill: GET /fleet/skill.

Serves the complete fleet-control-plane SKILL.md (docs/fleet-skill/SKILL.md) as
text/plain, 200, no redirect — the larrybrain pattern. Any agent that reads
markdown becomes a fleet CLIENT in one curl: enroll, reconcile, report runs,
harvest, and (with an operator key) drive placements.

Distinct from /skill (the marketplace skill served by skill_serve_routes.py):
this is the FLEET surface (the control plane), that is the CATALOG surface.

ROOT-level route (no /api prefix) so the bare URL works. Public (GET-only, no
write verb, no secrets in the body — the served file is a checked-in doc).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

fleet_skill_serve_router = APIRouter(tags=["fleet"])

# docs/fleet-skill/SKILL.md lives two levels up from app/.
FLEET_SKILL_MD_PATH = Path(__file__).resolve().parent.parent / "docs" / "fleet-skill" / "SKILL.md"


@lru_cache(maxsize=1)
def _fleet_skill_md() -> str:
    """Read the fleet-control-plane SKILL.md once (cached for process lifetime)."""
    return FLEET_SKILL_MD_PATH.read_text(encoding="utf-8")


@fleet_skill_serve_router.get("/fleet/skill", include_in_schema=False)
@fleet_skill_serve_router.get("/fleet/skill/", include_in_schema=False)
@fleet_skill_serve_router.get("/fleet/SKILL.md", include_in_schema=False)
def serve_fleet_skill() -> PlainTextResponse:
    """Serve the fleet-control-plane SKILL.md as text/plain (no redirect).

    An agent runs `curl -sL https://app.loopskill.io/fleet/skill -o SKILL.md`
    and gets a file it can load directly — turning it into a fleet client with
    the correct endpoint names, auth, install steps, and security notes.
    """
    return PlainTextResponse(
        content=_fleet_skill_md(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )
