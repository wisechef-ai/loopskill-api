#!/usr/bin/env python3
"""spotify_0608 Ph H — seed 10 editorial cookbooks owned by a SYSTEM account.

The cold-start "wheel" (premortem P1): editorial cookbooks ARE the wheel until
UGC population grows — Spotify launched on staff playlists. These 10 are owned
by a dedicated WiseChef SYSTEM account (NOT is_base=true — the base catalog is
sacrosanct, see loopclose_3005 invariant), each themed for an agent-builder
workflow, each published (slug + visibility='public') so they surface on the
discover feed, public pages, and leaderboards built in Ph B/F/G.

HERO (plan §4.1): "The Awakened Agent" (zero-to-agent) — summarize-cli (60s
cold-wow, no creds) + super-memory (depth/memory pull) + chef (Pro paywall
driver). Install → an agent that reads any link, remembers everything, and runs
itself, one MCP line.

DESIGN INVARIANTS (memory + loopclose_3005):
  - NEVER touch the is_base=true 'WiseChef Recipes Catalog' cookbook.
  - System account is a real User row (email 'editorial@wisechef.ai'), owner of
    every seeded cookbook — never owner-less.
  - Idempotent: re-running upserts by slug (no duplicate cookbooks, no
    duplicate memberships). Only real catalog slugs are attached; a missing
    slug is reported, never fabricated.

Run on prod:
    cd /home/wisechef/wiserecipes-api && ./venv/bin/python scripts/seed_editorial_cookbooks.py
Add --dry-run to preview without writing.
"""

from __future__ import annotations

import sys
from uuid import uuid4

# ── Editorial cookbook definitions — themes mapped to REAL catalog slugs ─────
# (verified against /api/skills/search on 2026-06-09). install_order is implicit
# in list order; the hero is first.
EDITORIAL_COOKBOOKS: list[dict] = [
    {
        "slug": "the-awakened-agent",
        "name": "The Awakened Agent",
        "description": (
            "Zero to autonomous in one install. Your agent reads any link, "
            "remembers everything across sessions, and runs its own daily ops "
            "loop. The 60-second wow: hand it a URL and get a clean digest with "
            "zero setup."
        ),
        "skills": ["summarize-cli", "super-memory", "chef"],
        "verified": True,
    },
    {
        "slug": "content-engine",
        "name": "The Content Engine",
        "description": (
            "A daily content marketing pipeline: ideate, draft, render, and "
            "voice. From blank page to publish-ready assets your agent produces "
            "on a schedule."
        ),
        "skills": ["creative-ideation", "hyperframes-video", "local-tts-kokoro", "summarize-cli"],
        "verified": True,
    },
    {
        "slug": "agent-fleet",
        "name": "The Agent Fleet",
        "description": (
            "Coordinate multiple agents like a team. Discord-native multi-agent "
            "coordination, shared memory, and an autonomous ops loop — the stack "
            "behind a self-running agent fleet."
        ),
        "skills": ["multi-agent-discord-coordination", "super-memory", "chef"],
        "verified": True,
    },
    {
        "slug": "scraping-pipeline",
        "name": "The Scraping Pipeline",
        "description": (
            "Turn the open web into structured data. Production-grade scraping, "
            "a search layer, and a data pipeline your agent runs end-to-end."
        ),
        "skills": ["scrapling-official", "tavily-search", "data-pipeline"],
    },
    {
        "slug": "incident-response",
        "name": "Incident Response",
        "description": (
            "When prod breaks at 2am, your agent triages first. Structured "
            "incident response, Railway diagnosis, and a CI-fix loop."
        ),
        "skills": ["incident-response", "railway-serverless-diagnose", "gh-fix-ci"],
    },
    {
        "slug": "code-review-bench",
        "name": "The Code-Review Bench",
        "description": (
            "A ruthless reviewer on tap. Clean-code and clean-architecture "
            "rubrics, a critical reviewer, and PR-draft generation — review "
            "discipline your agent applies on every diff."
        ),
        "skills": ["critical-code-reviewer", "code-review", "clean-architecture", "pr-draft"],
    },
    {
        "slug": "founder-toolkit",
        "name": "The Founder's Toolkit",
        "description": (
            "From idea to offer. Startup architecture, lean-startup experiments, "
            "the Mom Test for customer discovery, and irresistible-offer design."
        ),
        "skills": [
            "startup-architect",
            "lean-startup",
            "mom-test",
            "hundred-million-offers",
            "obviously-awesome",
        ],
    },
    {
        "slug": "mcp-builder-stack",
        "name": "The MCP Builder Stack",
        "description": (
            "Build the tools your agents use. An MCP-server builder, codebase "
            "knowledge graphs, and repo visualization — the meta-stack for "
            "agent-tooling work."
        ),
        "skills": ["mcp-builder", "gitnexus", "graphify", "repo-viz"],
    },
    {
        "slug": "sales-ops",
        "name": "Sales Ops",
        "description": (
            "Fill and work the pipeline. Cold outreach, proposal building, "
            "client reporting, and competitive intel — the revenue loop your "
            "agent runs."
        ),
        "skills": [
            "cold-outreach",
            "proposal-builder",
            "client-reporter",
            "customer-discovery-competitive-intel",
        ],
    },
    {
        "slug": "local-ai-lab",
        "name": "The Local AI Lab",
        "description": (
            "Run models on your own metal. llama.cpp inference, low-VRAM model "
            "selection, local TTS, and fast local transcription — a private, "
            "offline-capable AI stack."
        ),
        "skills": ["llama-cpp", "ollama-low-vram-model-pick", "local-tts-kokoro", "faster-whisper"],
    },
]

SYSTEM_EMAIL = "editorial@wisechef.ai"
SYSTEM_NAME = "WiseChef Editorial"


def _get_or_create_system_user(db, User):
    """Return the editorial SYSTEM user, creating it if absent. Never is_base."""
    u = db.query(User).filter(User.email == SYSTEM_EMAIL).first()
    if u is not None:
        return u
    u = User(
        id=uuid4(),
        github_id=900_000_000 + (abs(hash(SYSTEM_EMAIL)) % 90_000_000),
        email=SYSTEM_EMAIL,
        display_name=SYSTEM_NAME,
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def seed(dry_run: bool = False) -> int:
    from app.database import SessionLocal
    from app.models import Cookbook, CookbookSkill, Skill, User

    db = SessionLocal()
    created, updated, missing_slugs = 0, 0, []
    try:
        system = _get_or_create_system_user(db, User)

        # Cache real catalog slugs once so we never attach a fabricated skill.
        known = {
            row[0]
            for row in db.query(Skill.slug).filter(Skill.is_public.is_(True), Skill.is_archived.is_(False))
        }

        for spec in EDITORIAL_COOKBOOKS:
            slug = spec["slug"]
            cb = db.query(Cookbook).filter(Cookbook.slug == slug).first()
            is_new = cb is None
            if is_new:
                cb = Cookbook(id=uuid4(), name=spec["name"], slug=slug)
                db.add(cb)
                db.flush()
                created += 1
            else:
                updated += 1

            # Guard: NEVER convert the is_base catalog. (Defensive — slugs here
            # are editorial, but fail loud if a name ever collides with base.)
            if cb.is_base:
                print(f"REFUSING to mutate is_base cookbook for slug={slug}", file=sys.stderr)
                continue

            cb.name = spec["name"]
            cb.description = spec["description"]
            cb.cookbook_owner = system.id
            cb.visibility = "public"
            cb.is_verified = bool(spec.get("verified", False))

            # Attach real skills only; report (never fabricate) missing ones.
            for sk_slug in spec["skills"]:
                if sk_slug not in known:
                    missing_slugs.append(f"{slug}:{sk_slug}")
                    continue
                skill = db.query(Skill).filter(Skill.slug == sk_slug).first()
                if skill is None:
                    missing_slugs.append(f"{slug}:{sk_slug}")
                    continue
                exists = (
                    db.query(CookbookSkill)
                    .filter(
                        CookbookSkill.cookbook_id == cb.id,
                        CookbookSkill.skill_id == skill.id,
                    )
                    .first()
                )
                if exists is None:
                    db.add(CookbookSkill(cookbook_id=cb.id, skill_id=skill.id, source="custom-added"))

        if dry_run:
            print(f"[dry-run] would create={created} update={updated}")
            if missing_slugs:
                print(f"[dry-run] MISSING catalog slugs (skipped, not fabricated): {missing_slugs}")
            db.rollback()
            return 0

        db.commit()
        print(f"seed complete: created={created} updated={updated} system_user={system.email}")
        if missing_slugs:
            print(f"WARNING — missing catalog slugs (skipped, not fabricated): {missing_slugs}")
        # Verification read-back.
        n_public = (
            db.query(Cookbook)
            .filter(Cookbook.cookbook_owner == system.id, Cookbook.visibility == "public")
            .count()
        )
        print(f"verify: {n_public} public editorial cookbooks owned by {system.email}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(seed(dry_run="--dry-run" in sys.argv))
