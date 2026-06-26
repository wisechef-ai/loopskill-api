"""Well-known skills bridge — serve a public cookbook as an agentskills.io bundle.

onechrome follow-on (cookbook↔bundle compatibility, 2026-06-12).

A Recipes *cookbook* is a named, ordered set of skills. A Hermes/Claude-Code/etc.
*skill bundle* is, per the open agentskills.io standard, a set of skills a site
publishes at ``/.well-known/skills/index.json`` (the Vercel skills.sh discovery
convention). These are the SAME shape — a Recipes skill already carries
``name`` / ``description`` / a SKILL.md body.

This module closes the missing half: Recipes already CONSUMES external
``.well-known/skills`` endpoints (``app/services/federation_adapters.py``); here
it SERVES its own public cookbooks the same way. A fleet owner can then run::

    hermes skills install well-known:https://recipes.wisechef.ai/api/cookbooks/public/<slug>

…and the whole cookbook lands as native skills in any agentskills.io-compatible
agent. No proprietary manifest, no Hermes-specific code.

Two routes, both PUBLIC (no API key — discovery must work before an agent has a
key, matching the existing public skill-detail surface):

  GET  /api/cookbooks/public/{slug}/.well-known/skills/index.json
       → {"skills": [{"name", "description", "files": ["SKILL.md"]}, ...]}
         Lists EVERY skill in the cookbook (free + paid) so the caller sees the
         full bundle. Paid skills are flagged via a "tier" hint (non-standard but
         harmless extra key) but still listed.

  GET  /api/cookbooks/public/{slug}/.well-known/skills/{skill}/SKILL.md
       → text/markdown. For a FREE skill: the real readme body. For a PAID skill:
         a stub SKILL.md (title + description + locked-pointer) — the paid IP NEVER
         crosses this unauthenticated surface. This mirrors how the public skill
         detail page already withholds paid readme bodies.

Paywall invariant: the index reveals WHAT is in the bundle (names + descriptions
are already public on the cookbook page); only FREE bodies are served verbatim.
Installing a paid skill's real body still requires authenticated
``recipes_cookbook_install`` / a tier key.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Bundle

_h = APIRouter(tags=["bundles", "well-known"])  # prefix-free; dual-mounted below

# Tiers whose SKILL.md body is safe to serve verbatim over the unauthenticated
# well-known surface. Everything else gets a stub pointer.
_FREE_TIERS = {"free", None, ""}


def _is_free(skill) -> bool:
    """A skill's body is publicly serveable iff it is free."""
    tier = (skill.tier or "").lower()
    if tier in _FREE_TIERS:
        return True
    # is_free is an explicit override flag (nullable); honor a True.
    return bool(getattr(skill, "is_free", False))


def _resolve_public_cookbook(db: Session, slug: str) -> Bundle:
    cb = db.query(Bundle).filter(Bundle.slug == slug).first()
    if not cb or cb.visibility != "public":
        raise HTTPException(status_code=404, detail="cookbook_not_found")
    return cb


def _stub_skill_md(skill, cookbook_slug: str) -> str:
    """A non-leaking SKILL.md for a PAID skill.

    Carries the agentskills.io frontmatter (so the file is a valid skill the
    agent can register) plus a clear locked-body pointer. No paid content.
    """
    title = skill.title or skill.slug
    desc = (skill.description or "").replace("\n", " ").strip()
    tier = (skill.tier or "pro").lower().replace("_", "+")
    return (
        "---\n"
        f"name: {skill.slug}\n"
        f"description: {desc}\n"
        "license: proprietary\n"
        "metadata:\n"
        "  recipes:\n"
        f"    tier: {skill.tier or 'pro'}\n"
        "    locked: true\n"
        f"    cookbook: {cookbook_slug}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"> 🔒 **{tier} skill.** The full instructions for this skill are part of "
        f"the Recipes **{tier}** tier and are not served over the public bundle "
        "surface.\n\n"
        "## How to unlock\n\n"
        "Install this cookbook with an authenticated Recipes key and the real "
        "body is delivered:\n\n"
        "```\n"
        f'recipes_cookbook_install from "cookbook://{cookbook_slug}"\n'
        "```\n\n"
        f"Or subscribe at https://recipes.wisechef.ai/pricing and install "
        f"`{skill.slug}` directly.\n"
    )


@_h.get("/public/{slug}/.well-known/skills/index.json")
def cookbook_wellknown_index(slug: str, db: Session = Depends(get_db)) -> JSONResponse:
    """agentskills.io discovery index for a public cookbook.

    Public (no auth). 404 unless the cookbook is visibility='public'.
    """
    # Local import avoids a circular import at module load (bundle_routes
    # imports this router's host module in some app-factory orderings).
    from app.bundle_routes import _skills_for

    cb = _resolve_public_cookbook(db, slug)
    rows = _skills_for(db, cb.id, include_disabled=False)

    skills = []
    for _cs, skill in rows:
        entry = {
            "name": skill.slug,
            "description": (skill.description or skill.title or skill.slug),
            "files": ["SKILL.md"],
        }
        # Non-standard but harmless hint so a UI can see which entries
        # ship a real body vs a locked stub. agentskills.io consumers ignore
        # unknown keys.
        if not _is_free(skill):
            entry["locked"] = True
            entry["tier"] = skill.tier or "pro"
        skills.append(entry)

    body = {
        "skills": skills,
        # Extra metadata (ignored by strict consumers) so the bundle is
        # self-describing when fetched directly.
        "cookbook": {"slug": cb.slug, "name": cb.name, "skill_count": len(skills)},
    }
    return JSONResponse(body)


@_h.get("/public/{slug}/.well-known/skills/{skill_name}/SKILL.md")
def cookbook_wellknown_skill_md(
    slug: str, skill_name: str, db: Session = Depends(get_db)
) -> PlainTextResponse:
    """Serve one skill's SKILL.md from a public cookbook bundle.

    Public (no auth). FREE skill → real readme body. PAID skill → stub pointer
    (no paid IP crosses this surface). 404 if the skill is not in this cookbook.
    """
    from app.bundle_routes import _skills_for

    cb = _resolve_public_cookbook(db, slug)
    rows = _skills_for(db, cb.id, include_disabled=False)

    match = next((skill for _cs, skill in rows if skill.slug == skill_name), None)
    if match is None:
        raise HTTPException(status_code=404, detail="skill_not_in_cookbook")

    if _is_free(match) and (match.readme or "").strip():
        return PlainTextResponse(match.readme, media_type="text/markdown")

    # Paid (or free-but-empty-body): serve the non-leaking stub.
    return PlainTextResponse(_stub_skill_md(match, cb.slug), media_type="text/markdown")


# Dual-mount: bundle surface primary; /api/cookbooks kept as compat alias.  # compat-alias
router = APIRouter()
router.include_router(_h, prefix="/api/bundles")
router.include_router(_h, prefix="/api/cookbooks")  # compat-alias
