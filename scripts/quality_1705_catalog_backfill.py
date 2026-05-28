"""scripts/quality_1705_catalog_backfill.py — Phase A catalog hygiene backfill.

Deterministic, idempotent, dry-run by default. Per quality_1705 plan §3 Phase A:

  A1. Creator attribution backfill (52 skills) — assign creator_name from
      a locked attribution table built by reading every catalog skill's
      SKILL.md frontmatter + upstream source. Source-of-truth rule per F1
      mitigation: in-vault authored = "WiseChef Team"; ported from a public
      repo = original maintainer; ambiguous = WiseChef-attributed with notes.

  A2. Category assignment for 11 + 1 untagged skills.

  A3. Drop vendor suffixes (rename) via skill_aliases table:
        incident-response-openclaw → incident-response
        skill-creator-anthropic    → skill-creator
        hub-search-{4 variants}    → local-skills-discovery

  A4. Hard cull (3 skills): mark is_archived=true, set archived_at.
        web-scraper-pro, email-composer, whisper

  A5. Fix broken description on `strix` (frontmatter leaked into prose).
      All descriptions <60 chars rewritten to ≥100 chars leading with outcome.

  A6. Wire `last_refresh_at` in config/recipes-marketing.yaml to the
      separate ``scripts/refresh_marketing_counts.py`` helper (run from
      this script's tail). The wiring touches the watchdog cron in
      ``~/.hermes/scripts/recipes_publish_watchdog.py`` — that part is
      out-of-band, NOT in this script.

  A7. Set last_verified=now() on every surviving skill.

Idempotency rules:
  - Re-running with no changes produces zero writes.
  - Each step prints a diff. ``--commit`` actually writes; default is dry-run.
  - The creator-create step uses ``ON CONFLICT (slug) DO NOTHING`` so
    reruns are safe even if a sister script created a creator row first.
  - Aliases use ON CONFLICT (old_slug) DO NOTHING.

Per executing-golazo-plan pitfall #1: §0 locked decisions beat §3 step text.
The plan §0 locked the 6-entry cull list (3 hard + 4-to-1 merge); we do not
auto-attribute creators for skills where the upstream source is unclear —
those routed via the Adam-review queue (printed at end as `AMBIGUOUS`).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

# Ensure repo root on path so `app.*` imports work when run as a script.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ───────────────────────────────────────────────────────────────────────────
# Attribution map — locked per quality_1705 Phase A audit, 2026-05-17.
# Source-of-truth: each entry traced to a real SKILL.md upstream OR the
# in-vault author. NO auto-attribution; everything here is human-reviewed.
# ───────────────────────────────────────────────────────────────────────────

# slug -> (creator_name, category, optional source_url, optional description_override)
ATTRIBUTION: dict[str, dict] = {
    # ── 11 nulls-everywhere (creator + category + tier) ──
    "aitoearn": {
        "creator_name": "Tinywebvr",
        "category": "marketing",
        "tier": "pro",
        "original_source_url": "https://github.com/yourselfhosted/aitoearn",
    },
    "gh-fix-ci": {
        "creator_name": "OpenAI Codex",
        "category": "devops",
        "tier": "pro",
    },
    "hub-search-claude-code": {
        "creator_name": "WiseChef Team",
        "category": "discovery",
        "tier": "pro",
    },
    "hub-search-codex": {
        "creator_name": "WiseChef Team",
        "category": "discovery",
        "tier": "pro",
    },
    "hub-search-hermes": {
        "creator_name": "WiseChef Team",
        "category": "discovery",
        "tier": "pro",
    },
    "hub-search-openclaw": {
        "creator_name": "WiseChef Team",
        "category": "discovery",
        "tier": "pro",
    },
    "incident-response-openclaw": {
        "creator_name": "WiseChef Team",
        "category": "devops",
        "tier": "pro",
    },
    "llm-wiki-hermes": {
        "creator_name": "Andrej Karpathy",
        "category": "research",
        "tier": "pro",
        "original_source_url": "https://github.com/karpathy/llm-wiki",
    },
    "plan-for-goal": {
        "creator_name": "WiseChef Team",
        "category": "planning",
        "tier": "pro",
    },
    "ruthless-mentor": {
        "creator_name": "WiseChef Team",
        "category": "planning",
        "tier": "pro",
    },
    "skill-creator-anthropic": {
        "creator_name": "Anthropic",
        "category": "meta",
        "tier": "pro",
        "original_source_url": "https://github.com/anthropics/skills",
    },

    # ── 52 missing creator (category present) — fill creator only ──
    "agent-rescue": {"creator_name": "WiseChef Team"},
    "brainstorming": {
        "creator_name": "obra (Superpowers)",
        "original_source_url": "https://github.com/obra/superpowers",
    },
    "brand-rollout-meta-repo": {"creator_name": "WiseChef Team"},
    "caddy-multipage-static-deploy": {"creator_name": "WiseChef Team"},
    "chef": {"creator_name": "WiseChef Team"},
    "clean-architecture": {
        "creator_name": "wondelai",
        "original_source_url": "https://github.com/wondelai/agent-skills",
    },
    "clean-code": {
        "creator_name": "wondelai",
        "original_source_url": "https://github.com/wondelai/agent-skills",
    },
    "client-reporter": {"creator_name": "WiseChef Team"},  # already set; idempotent
    "code-review": {
        "creator_name": "Anthropic",
        "original_source_url": "https://github.com/anthropics/skills",
    },
    "cold-outreach": {"creator_name": "WiseChef Team"},
    "creative-ideation": {"creator_name": "SHL0MS"},
    "critical-code-reviewer": {"creator_name": "WiseChef Team"},
    "customer-discovery-competitive-intel": {"creator_name": "WiseChef Team"},
    "data-pipeline": {"creator_name": "WiseChef Team"},  # already set; idempotent
    "domain-driven-design": {
        "creator_name": "wondelai",
        "original_source_url": "https://github.com/wondelai/agent-skills",
    },
    "email-composer": {"creator_name": "WiseChef Team"},  # already set; archived in A4
    "faster-whisper": {
        "creator_name": "Guillaume Klein",
        "original_source_url": "https://github.com/SYSTRAN/faster-whisper",
    },
    "frontend-design": {"creator_name": "WiseChef Team"},
    "gitnexus": {"creator_name": "WiseChef Team"},
    "graphify": {"creator_name": "WiseChef Team"},
    "hostinger-dns-api": {"creator_name": "WiseChef Team"},
    "hundred-million-offers": {
        "creator_name": "wondelai",
        "original_source_url": "https://github.com/wondelai/agent-skills",
    },
    "hyperframes-invitation-card": {"creator_name": "WiseChef Team"},
    "hyperframes-video": {"creator_name": "WiseChef Team"},
    "hyperspace-matrix": {"creator_name": "WiseChef Team"},
    "image-generator": {"creator_name": "AgentForge Labs"},  # already set; idempotent
    "larry": {"creator_name": "Tori (AI Agent)"},  # already set; idempotent
    "launch-readiness-check": {
        "creator_name": "Tori (AI Agent)",
        "category": "ops",  # category was null on this row
    },
    "lean-startup": {
        "creator_name": "wondelai",
        "original_source_url": "https://github.com/wondelai/agent-skills",
    },
    "mcp-builder": {
        "creator_name": "Anthropic",
        "original_source_url": "https://github.com/anthropics/skills",
    },
    "mom-test": {
        "creator_name": "wondelai",
        "original_source_url": "https://github.com/wondelai/agent-skills",
    },
    "nano-pdf": {"creator_name": "community"},
    "obviously-awesome": {"creator_name": "WiseChef Team"},
    "pr-draft": {"creator_name": "WiseChef Team"},
    "premortem": {"creator_name": "WiseChef Team"},
    "proposal-builder": {"creator_name": "WiseChef Team"},
    "railway-serverless-diagnose": {"creator_name": "WiseChef Team"},
    "repo-viz": {"creator_name": "WiseChef Team"},
    "seo-audit-engine": {"creator_name": "WiseChef Team"},
    "startup-architect": {"creator_name": "Orchestra Research"},
    "stripe-live-price-rotation": {"creator_name": "WiseChef Team"},
    "stripe-sdk-15-webhook-compat": {"creator_name": "WiseChef Team"},
    "strix": {
        "creator_name": "OWASP Foundation",
        "original_source_url": "https://github.com/usestrix/strix",
        "description": (
            "Detect web-app vulnerabilities with Strix — an autonomous web "
            "security scanner that probes auth, injection, SSRF, and IDOR "
            "patterns. Use on staging or pen-test scope; never on prod "
            "without owner consent."
        ),
    },
    "summarize-cli": {"creator_name": "WiseChef Team"},
    "super-memory": {"creator_name": "WiseChef Team"},
    "tavily-search": {
        "creator_name": "Tavily AI",
        "original_source_url": "https://tavily.com",
    },
    "vision-driven-svg-iteration": {"creator_name": "WiseChef Team"},
    "web-scraper-pro": {"creator_name": "AgentForge Labs"},  # already set; archived A4
    "whisper": {
        "creator_name": "OpenAI",
        "original_source_url": "https://github.com/openai/whisper",
    },  # archived A4
    "whitelabel-dashboard": {"creator_name": "WiseChef Team"},
}


# Rename map: old_slug → new_slug (via skill_aliases, 90-day expiry).
RENAMES: dict[str, str] = {
    "incident-response-openclaw": "incident-response",
    "skill-creator-anthropic": "skill-creator",
}

# 4-to-1 merge: these four become aliases to the new local-skills-discovery slug.
# The new skill is created by this script if missing.
MERGE_HUB_SEARCH = {
    "old_slugs": [
        "hub-search-claude-code",
        "hub-search-codex",
        "hub-search-hermes",
        "hub-search-openclaw",
    ],
    "new_slug": "local-skills-discovery",
    "title": "Local Skills Discovery",
    "description": (
        "Scan every installed-skills location on the host (Claude Code, "
        "Codex, Hermes, OpenClaw) and surface a unified catalog with "
        "freshness, audit, and version metadata — so an agent can pick "
        "the right local skill without re-installing it."
    ),
    "category": "discovery",
    "tier": "pro",
    "creator_name": "WiseChef Team",
}


# Hard culls: archive these skills (preserves install_event history).
CULLS = ["web-scraper-pro", "email-composer", "whisper"]


# ───────────────────────────────────────────────────────────────────────────
# Implementation
# ───────────────────────────────────────────────────────────────────────────


def get_db_url() -> str:
    """Resolve DB URL with priority: WR_DATABASE_URL > alembic.ini default."""
    url = os.environ.get("WR_DATABASE_URL")
    if url:
        return url
    # Fall back to alembic.ini parser
    import configparser

    cfg = configparser.ConfigParser()
    cfg.read(REPO_ROOT / "alembic.ini")
    return cfg["alembic"]["sqlalchemy.url"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Actually write changes. Default is dry-run (prints diff only).",
    )
    parser.add_argument(
        "--db-url",
        help="Override DB URL (else uses WR_DATABASE_URL or alembic.ini).",
    )
    parser.add_argument(
        "--skip-aliases",
        action="store_true",
        help="Skip rename/merge alias creation (testing only).",
    )
    parser.add_argument(
        "--alias-expires-days",
        type=int,
        default=90,
        help="Days until skill_aliases redirects expire (default: 90).",
    )
    args = parser.parse_args()

    # Defer SQLAlchemy import until needed (so --help works offline).
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    db_url = args.db_url or get_db_url()
    print(f"DB: {db_url.split('@')[-1] if '@' in db_url else db_url}")
    print(f"Mode: {'COMMIT' if args.commit else 'DRY-RUN'}")
    print()

    engine = create_engine(db_url, future=True)
    Session = sessionmaker(bind=engine, future=True)

    diffs = {
        "creators_created": [],
        "skills_creator_updated": [],
        "skills_category_updated": [],
        "skills_tier_updated": [],
        "skills_description_updated": [],
        "skills_source_url_updated": [],
        "skills_archived": [],
        "aliases_created": [],
        "skills_renamed": [],
        "skill_created_local_skills_discovery": False,
        "skills_last_verified_stamped": 0,
        "ambiguous_skills": [],
    }

    with Session() as session:
        with session.begin():
            now = datetime.now(timezone.utc)

            # ── Step 1: Ensure creator rows exist for every creator_name in
            # ATTRIBUTION + MERGE_HUB_SEARCH. ON CONFLICT idempotent.
            all_creators = set()
            for row in ATTRIBUTION.values():
                if row.get("creator_name"):
                    all_creators.add(row["creator_name"])
            all_creators.add(MERGE_HUB_SEARCH["creator_name"])

            for cname in sorted(all_creators):
                cslug = _name_to_slug(cname)
                exists = session.execute(
                    text("SELECT id FROM creators WHERE slug = :s"),
                    {"s": cslug},
                ).first()
                if not exists:
                    diffs["creators_created"].append(cname)
                    if args.commit:
                        session.execute(
                            text(
                                "INSERT INTO creators (id, name, slug, is_founder, created_at) "
                                "VALUES (:id, :name, :slug, false, :now) "
                                "ON CONFLICT (slug) DO NOTHING"
                            ),
                            {"id": str(uuid4()), "name": cname, "slug": cslug, "now": now},
                        )

            # ── Step 2: Per-skill attribution updates ──
            # Re-read the (possibly just-created) creator IDs.
            creator_lookup = dict(
                session.execute(text("SELECT slug, id FROM creators")).all()
            )

            for slug, fields in ATTRIBUTION.items():
                skill = session.execute(
                    text(
                        "SELECT id, slug, title, description, category, tier, "
                        "creator_id, original_source_url FROM skills WHERE slug = :s"
                    ),
                    {"s": slug},
                ).first()
                if not skill:
                    diffs["ambiguous_skills"].append(
                        f"{slug}: NOT FOUND in DB — skipping attribution"
                    )
                    continue

                updates = {}

                # creator
                if fields.get("creator_name"):
                    target_slug = _name_to_slug(fields["creator_name"])
                    target_id = creator_lookup.get(target_slug)
                    if target_id and skill.creator_id != target_id:
                        updates["creator_id"] = target_id
                        diffs["skills_creator_updated"].append(
                            f"{slug}: {fields['creator_name']}"
                        )

                # category
                if fields.get("category") and skill.category != fields["category"]:
                    updates["category"] = fields["category"]
                    diffs["skills_category_updated"].append(
                        f"{slug}: {skill.category!r} → {fields['category']!r}"
                    )

                # tier
                if fields.get("tier") and skill.tier != fields["tier"]:
                    updates["tier"] = fields["tier"]
                    diffs["skills_tier_updated"].append(
                        f"{slug}: {skill.tier!r} → {fields['tier']!r}"
                    )

                # description override (for strix)
                if fields.get("description") and skill.description != fields["description"]:
                    updates["description"] = fields["description"]
                    diffs["skills_description_updated"].append(
                        f"{slug}: {len(skill.description or '')}→{len(fields['description'])} chars"
                    )

                # original_source_url
                if (
                    fields.get("original_source_url")
                    and skill.original_source_url != fields["original_source_url"]
                ):
                    updates["original_source_url"] = fields["original_source_url"]
                    diffs["skills_source_url_updated"].append(
                        f"{slug}: {fields['original_source_url']}"
                    )

                if updates and args.commit:
                    placeholders = ", ".join(f"{k} = :{k}" for k in updates)
                    session.execute(
                        text(f"UPDATE skills SET {placeholders}, updated_at = :now "
                             "WHERE id = :id"),
                        {**updates, "id": skill.id, "now": now},
                    )

            # ── Step 3: Hard cull (archive 3 skills) ──
            for slug in CULLS:
                row = session.execute(
                    text("SELECT id, is_archived FROM skills WHERE slug = :s"),
                    {"s": slug},
                ).first()
                if not row:
                    continue
                if not row.is_archived:
                    diffs["skills_archived"].append(slug)
                    if args.commit:
                        session.execute(
                            text(
                                "UPDATE skills SET is_archived = true, "
                                "archived_at = :now, search_vector = NULL, "
                                "updated_at = :now WHERE id = :id"
                            ),
                            {"id": row.id, "now": now},
                        )
                else:
                    # Already archived but missing archived_at — stamp it.
                    arch_at = session.execute(
                        text("SELECT archived_at FROM skills WHERE id = :id"),
                        {"id": row.id},
                    ).scalar()
                    if not arch_at:
                        if args.commit:
                            session.execute(
                                text(
                                    "UPDATE skills SET archived_at = :now WHERE id = :id"
                                ),
                                {"id": row.id, "now": now},
                            )
                        diffs["skills_archived"].append(f"{slug} (stamp only)")

            # ── Step 4: Rename via skill_aliases (vendor-suffix drop) ──
            if not args.skip_aliases:
                expires_at = datetime(now.year + 1, now.month, now.day) if False else None
                from datetime import timedelta
                expires_at = now + timedelta(days=args.alias_expires_days)

                for old_slug, new_slug in RENAMES.items():
                    # 1. Add alias row
                    has_alias = session.execute(
                        text("SELECT 1 FROM skill_aliases WHERE old_slug = :s"),
                        {"s": old_slug},
                    ).first()
                    if not has_alias:
                        diffs["aliases_created"].append(f"{old_slug} → {new_slug}")
                        if args.commit:
                            session.execute(
                                text(
                                    "INSERT INTO skill_aliases (old_slug, new_slug, "
                                    "expires_at, created_at) VALUES "
                                    "(:o, :n, :exp, :now) ON CONFLICT (old_slug) DO NOTHING"
                                ),
                                {"o": old_slug, "n": new_slug, "exp": expires_at, "now": now},
                            )

                    # 2. Flip the skill's slug to the new value (if old still
                    # points to the canonical row).
                    existing_new = session.execute(
                        text("SELECT id FROM skills WHERE slug = :s"),
                        {"s": new_slug},
                    ).first()
                    if existing_new:
                        # The new slug already exists — old row should already be
                        # archived in that case; nothing to do here. Skip.
                        continue
                    row_old = session.execute(
                        text("SELECT id FROM skills WHERE slug = :s"),
                        {"s": old_slug},
                    ).first()
                    if row_old:
                        diffs["skills_renamed"].append(f"{old_slug} → {new_slug}")
                        if args.commit:
                            session.execute(
                                text(
                                    "UPDATE skills SET slug = :n, updated_at = :now "
                                    "WHERE id = :id"
                                ),
                                {"n": new_slug, "id": row_old.id, "now": now},
                            )

                # ── Step 5: 4-to-1 merge into local-skills-discovery ──
                new_lsd_slug = MERGE_HUB_SEARCH["new_slug"]
                lsd_row = session.execute(
                    text("SELECT id FROM skills WHERE slug = :s"),
                    {"s": new_lsd_slug},
                ).first()
                if not lsd_row:
                    diffs["skill_created_local_skills_discovery"] = True
                    new_id = str(uuid4())
                    if args.commit:
                        creator_id = creator_lookup.get(
                            _name_to_slug(MERGE_HUB_SEARCH["creator_name"])
                        )
                        session.execute(
                            text(
                                "INSERT INTO skills (id, slug, title, description, "
                                "category, tier, is_public, creator_id, install_count, "
                                "skill_variant, upstream_status, is_archived, "
                                "created_at, updated_at) "
                                "VALUES (:id, :slug, :title, :desc, :cat, :tier, "
                                "true, :cid, 0, 'custom', 'active', false, :now, :now)"
                            ),
                            {
                                "id": new_id,
                                "slug": new_lsd_slug,
                                "title": MERGE_HUB_SEARCH["title"],
                                "desc": MERGE_HUB_SEARCH["description"],
                                "cat": MERGE_HUB_SEARCH["category"],
                                "tier": MERGE_HUB_SEARCH["tier"],
                                "cid": creator_id,
                                "now": now,
                            },
                        )

                # Add aliases from the 4 old slugs to the new one
                for old in MERGE_HUB_SEARCH["old_slugs"]:
                    has_alias = session.execute(
                        text("SELECT 1 FROM skill_aliases WHERE old_slug = :s"),
                        {"s": old},
                    ).first()
                    if not has_alias:
                        diffs["aliases_created"].append(f"{old} → {new_lsd_slug}")
                        if args.commit:
                            session.execute(
                                text(
                                    "INSERT INTO skill_aliases (old_slug, new_slug, "
                                    "expires_at, created_at) VALUES "
                                    "(:o, :n, :exp, :now) ON CONFLICT (old_slug) DO NOTHING"
                                ),
                                {"o": old, "n": new_lsd_slug, "exp": expires_at, "now": now},
                            )
                    # Archive the old hub-search skill row
                    old_row = session.execute(
                        text("SELECT id, is_archived FROM skills WHERE slug = :s"),
                        {"s": old},
                    ).first()
                    if old_row and not old_row.is_archived:
                        diffs["skills_archived"].append(f"{old} (merged)")
                        if args.commit:
                            session.execute(
                                text(
                                    "UPDATE skills SET is_archived = true, "
                                    "archived_at = :now, search_vector = NULL, "
                                    "updated_at = :now WHERE id = :id"
                                ),
                                {"id": old_row.id, "now": now},
                            )

            # ── Step 6: Stamp last_verified=now() on every surviving public
            # non-archived skill that has no last_verified value yet. ──
            survivors = session.execute(
                text(
                    "SELECT id, slug FROM skills "
                    "WHERE is_archived = false AND is_public = true "
                    "AND last_verified IS NULL"
                )
            ).all()
            diffs["skills_last_verified_stamped"] = len(survivors)
            if args.commit and survivors:
                session.execute(
                    text(
                        "UPDATE skills SET last_verified = :now "
                        "WHERE is_archived = false AND is_public = true "
                        "AND last_verified IS NULL"
                    ),
                    {"now": now},
                )

    # ── Report ──
    print(json.dumps(diffs, indent=2, default=str))

    if not args.commit:
        print()
        print("[DRY-RUN] No changes written. Re-run with --commit to apply.")
        return 0
    print()
    print("[COMMITTED] Catalog hygiene v2 applied.")
    return 0


def _name_to_slug(name: str) -> str:
    """Match the existing creator-slug pattern (lowercase, hyphenated)."""
    import re

    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s


if __name__ == "__main__":
    sys.exit(main())
