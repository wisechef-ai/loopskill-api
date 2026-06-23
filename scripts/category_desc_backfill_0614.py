"""
Catalog category + short-description backfill — 2026-06-14 (atomic-habits rank-8).

Patches 12 skills that have no category set (12 nulls visible in
/api/skills/search) and 4 skills with descriptions under 80 chars.

Extended 2026-06-15: added recipes-cookbook-reconcile (ops) which was
omitted from the original 11-entry map.

Canonical decision log:
  - Category values use the existing catalog vocabulary (productivity, research,
    content, ops, agency) — not inventing new ones.
  - Short desc rewrites are problem-first: what problem does the buyer solve?
  - This is a copy-only change: no tier, no Stripe, no pricing touched.

Usage (run from the repo root on the DB host, or locally with a tunnel):
  DATABASE_URL=postgresql://... python scripts/category_desc_backfill_0614.py [--dry-run]

Exit codes:
  0 = success (or --dry-run completed cleanly)
  1 = error
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Skill  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("category-desc-backfill")

# ---------------------------------------------------------------------------
# Catalog vocabulary used by existing skills (grep from /api/skills/search).
# Only assign from this set — do not invent new categories.
# productivity(15), research(12), ops(7), content(6), discovery(4),
# agency(4), data(3), marketing(3), planning(2), devops(2), automation(1), meta(1)
# ---------------------------------------------------------------------------

CATEGORY_PATCHES: dict[str, str] = {
    "local-tts-kokoro": "productivity",          # TTS → productivity tool
    "scrapling-official": "research",            # scraping → research
    "ascii-video": "content",                   # creative video → content
    "comfyui": "content",                       # image/video gen → content
    "cognee": "ops",                            # knowledge graph ops
    "ollama-low-vram-model-pick": "ops",        # model selection → ops
    "manim-video": "content",                  # animation → content
    "llama-cpp": "ops",                        # inference runtime → ops
    "obsidian-livesync-bridge": "ops",         # sync bridge → ops
    "maestro": "agency",                       # agent framework → agency
    "framework-v0": "agency",                 # agent bootstrap → agency
    "recipes-cookbook-reconcile": "ops",       # cookbook reconciliation → ops
}

# Short description rewrites (problem-first, ≥80 chars each).
# Only patch slugs that are currently < 80 chars.
DESC_PATCHES: dict[str, str] = {
    "ascii-video": (
        "Convert any video or audio file to colored ASCII art MP4/GIF animations. "
        "Supports frame extraction, palette mapping, and batch rendering for "
        "terminal-art creative projects."
    ),
    "manim-video": (
        "Create 3Blue1Brown-style math and algorithm explanation videos with Manim "
        "Community Edition. Render LaTeX equations, geometric proofs, and animated "
        "code walkthroughs programmatically."
    ),
    "llama-cpp": (
        "Run quantized GGUF models locally using llama.cpp for CPU/GPU inference. "
        "Includes HuggingFace Hub model discovery, context-length tuning, and "
        "OpenAI-compatible server mode."
    ),
    "framework-v0": (
        "One-command bootstrap that installs the Maestro solo-operator agent "
        "framework plus 4 core dependencies: morning-brief, marketing-engine, "
        "code-dispatch, and health-watchdog."
    ),
}


def run(dry_run: bool = False) -> int:
    db: Session = SessionLocal()
    try:
        category_hits = 0
        desc_hits = 0
        errors = 0

        all_slugs = set(CATEGORY_PATCHES) | set(DESC_PATCHES)
        stmt = select(Skill).where(Skill.slug.in_(all_slugs))
        skills = db.execute(stmt).scalars().all()
        found_slugs = {s.slug for s in skills}
        missing = all_slugs - found_slugs
        if missing:
            log.warning("Slugs not found in DB (skip): %s", sorted(missing))

        for skill in skills:
            changed = False

            # Category patch
            new_cat = CATEGORY_PATCHES.get(skill.slug)
            if new_cat and (not skill.category or skill.category != new_cat):
                log.info(
                    "category %s: %r → %r",
                    skill.slug,
                    skill.category,
                    new_cat,
                )
                if not dry_run:
                    skill.category = new_cat
                category_hits += 1
                changed = True

            # Desc patch (only if current desc < 80 chars)
            new_desc = DESC_PATCHES.get(skill.slug)
            if new_desc and len(skill.description or "") < 80:
                log.info(
                    "description %s: %d chars → %d chars",
                    skill.slug,
                    len(skill.description or ""),
                    len(new_desc),
                )
                if not dry_run:
                    skill.description = new_desc
                desc_hits += 1
                changed = True

            if changed and not dry_run:
                db.add(skill)

        if not dry_run:
            db.commit()
            log.info("Committed: %d category patches, %d desc patches", category_hits, desc_hits)
        else:
            log.info(
                "DRY RUN — would patch: %d categories, %d descriptions",
                category_hits,
                desc_hits,
            )

        return 0
    except Exception as exc:  # Rationale: top-level script, surface all errors
        log.error("Backfill failed: %s", exc, exc_info=True)
        db.rollback()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Category + description backfill 2026-06-14")
    parser.add_argument("--dry-run", action="store_true", help="Log changes without committing")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run))
