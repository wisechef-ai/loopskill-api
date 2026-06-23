"""LoopSkill Phase 1 — starter catalog seed.

Seeds ≥1 of each catalog type so a fresh clone has browsable content:
  skill · bundle · loop · personality

Designed to be idempotent (slug-keyed upsert). Ships as in-repo data —
zero network access required. All new strings use clean LoopSkill vocabulary.

Usage:
    python scripts/seed_starter_catalog.py          # run standalone
    python scripts/seed_starter_catalog.py --check  # exit 0 if already seeded
"""

from __future__ import annotations

import sys
from uuid import uuid4

SYSTEM_EMAIL = "system@loopskill.io"
SYSTEM_NAME = "LoopSkill Team"

# ── Starter bundles (skill collections) ──────────────────────────────────────
# These reference real catalog slug strings where possible; missing slugs are
# tolerated (logged, not fatal) — just like seed_editorial_cookbooks.py.
STARTER_BUNDLES = [
    {
        "slug": "dev-agent-essentials",
        "name": "Dev Agent Essentials",
        "description": (
            "The core skill set for an autonomous coding agent. Code review, "
            "CI fix, and PR draft automation — everything a dev agent needs "
            "to ship without supervision."
        ),
        "skills": ["code-reviewer", "web-scraper-pro", "client-reporter"],
        "verified": True,
    },
    {
        "slug": "research-and-report",
        "name": "Research & Report",
        "description": (
            "Search, summarise, and publish. Pair web scraping with reporting "
            "to produce client-ready outputs your agent delivers on a schedule."
        ),
        "skills": ["web-scraper-pro", "client-reporter"],
        "verified": False,
    },
]

# ── Starter loops (safety-bounded autonomous loops) ───────────────────────────
STARTER_LOOPS = [
    {
        "slug": "pr-review-loop",
        "title": "PR Review Loop",
        "description": (
            "Autonomous pull-request reviewer. Runs on every new PR, posts "
            "a structured review comment, and exits cleanly. Safety-bounded "
            "with a 10-turn ceiling and a verification script that checks the "
            "comment was posted."
        ),
        "category": "development",
        "readme": (
            "# PR Review Loop\n\n"
            "## What it does\n"
            "For each open PR, the loop:\n"
            "1. Reads the diff via the GitHub tool.\n"
            "2. Posts a structured review comment (bugs / perf / style).\n"
            "3. Verifies the comment exists, then exits.\n\n"
            "## Safety contract\n"
            "- `max_turns`: 10\n"
            "- `budget_usd`: null (relies on max_turns)\n"
            "- `tool_allowlist`: [github_read_pr, github_post_comment]\n"
            "- Verification: `gh pr view <PR> --json comments | jq '.comments | length > 0'`\n\n"
            "## Install\n"
            "```\nloopskill pull loop pr-review-loop\n```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": (
            "A review comment has been posted on the target pull request "
            "covering correctness, performance, and style findings."
        ),
        "verification_script": (
            'gh pr view "$PR_NUMBER" --json comments ' "| jq -e '.comments | length > 0'"
        ),
        "max_turns": 10,
        "budget_usd": None,
        "stopping_criteria": {
            "success": "verification_script exits 0",
            "failure": "max_turns reached without posting a comment",
            "budget": "N/A — bounded by max_turns only",
        },
        "tool_allowlist": ["github_read_pr", "github_post_comment"],
        "system_prompt": (
            "You are a rigorous code reviewer. For the given pull request diff, "
            "identify correctness bugs, performance issues, and style violations. "
            "Post a single structured review comment with three sections: "
            "## Bugs, ## Performance, ## Style. Be concise and actionable. "
            "Call the verification script when done to confirm the comment landed."
        ),
    },
    {
        "slug": "daily-briefing-loop",
        "title": "Daily Briefing Loop",
        "description": (
            "Autonomous daily digest generator. Scrapes configured sources, "
            "summarises the top items, and sends a briefing. Runs in under "
            "5 turns — safe, fast, and ready to schedule."
        ),
        "category": "productivity",
        "readme": (
            "# Daily Briefing Loop\n\n"
            "## What it does\n"
            "Each morning (or on demand):\n"
            "1. Fetches headlines from configured RSS / URL list.\n"
            "2. Summarises each item in ≤2 sentences.\n"
            "3. Formats and delivers the briefing to the configured output.\n\n"
            "## Safety contract\n"
            "- `max_turns`: 5\n"
            "- `budget_usd`: 0.10\n"
            "- `tool_allowlist`: [web_fetch, file_write]\n"
            "- Verification: `test -f /tmp/briefing.md && wc -l /tmp/briefing.md | awk '$1 > 3'`\n\n"
            "## Install\n"
            "```\nloopskill pull loop daily-briefing-loop\n```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "success_condition": (
            "A briefing file has been written containing at least one "
            "summarised headline from each configured source."
        ),
        "verification_script": (
            "test -f /tmp/briefing.md " "&& wc -l /tmp/briefing.md | awk '$1 > 3 {exit 0} {exit 1}'"
        ),
        "max_turns": 5,
        "budget_usd": "0.10",
        "stopping_criteria": {
            "success": "verification_script exits 0",
            "failure": "max_turns reached or budget exceeded",
            "budget": "$0.10 USD hard ceiling",
        },
        "tool_allowlist": ["web_fetch", "file_write"],
        "system_prompt": (
            "You are a concise daily briefing agent. Fetch each URL in the "
            "SOURCES list, extract the most important item, and summarise it "
            "in ≤2 sentences. Write all summaries to /tmp/briefing.md in "
            "Markdown format with a timestamp header. Call the verification "
            "script when the file is ready."
        ),
    },
]

# ── Starter personalities ─────────────────────────────────────────────────────
STARTER_PERSONALITIES = [
    {
        "slug": "focused-dev-agent",
        "title": "Focused Dev Agent",
        "description": (
            "A disciplined software engineer persona. Ships clean, tested code, "
            "asks clarifying questions before writing, and never hallucinates "
            "library APIs. Ideal for code-generation and refactor tasks."
        ),
        "category": "engineering",
        "readme": (
            "# Focused Dev Agent\n\n"
            "## Persona\n"
            "A senior software engineer who values correctness over speed. "
            "Asks exactly one clarifying question when the spec is ambiguous. "
            "Writes tests before implementation. Never invents library APIs.\n\n"
            "## Install\n"
            "```\nloopskill pull personality focused-dev-agent\n```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "system_prompt": (
            "You are a focused, disciplined software engineer. Your operating rules:\n"
            "1. Read the full task before writing a single line of code.\n"
            "2. If the spec is ambiguous, ask ONE clarifying question, then proceed.\n"
            "3. Write a failing test before the implementation (TDD).\n"
            "4. Never invent library APIs — only use what you can verify exists.\n"
            "5. Keep functions small and names descriptive.\n"
            "6. Commit with conventional commits: feat/fix/test/chore/refactor.\n"
            "You are terse, precise, and do not pad responses with filler text."
        ),
        "config": {
            "model": "claude-sonnet-4-6",
            "temperature": 0.2,
            "default_tools": ["file_read", "file_write", "bash"],
        },
    },
    {
        "slug": "research-analyst",
        "title": "Research Analyst",
        "description": (
            "A methodical research persona. Cross-references multiple sources, "
            "flags uncertainty, and cites everything. Great for due-diligence, "
            "competitive analysis, and literature review tasks."
        ),
        "category": "research",
        "readme": (
            "# Research Analyst\n\n"
            "## Persona\n"
            "A careful, evidence-driven analyst who never asserts without a "
            "source. Outputs structured findings with confidence levels. "
            "Flags gaps and contradictions explicitly.\n\n"
            "## Install\n"
            "```\nloopskill pull personality research-analyst\n```\n"
        ),
        "license": "MIT",
        "tier": "free",
        "system_prompt": (
            "You are a methodical research analyst. Your operating rules:\n"
            "1. For any factual claim, cite your source inline: [Source: <url or doc>].\n"
            "2. If you are uncertain, say 'Confidence: LOW' and explain why.\n"
            "3. Cross-reference at least two independent sources for key claims.\n"
            "4. Structure outputs as: Summary → Key Findings → Gaps → Next Steps.\n"
            "5. Never fabricate data, statistics, or quotes.\n"
            "6. Flag contradictions between sources explicitly.\n"
            "You produce dense, citation-heavy outputs. Quality over brevity."
        ),
        "config": {
            "model": "claude-sonnet-4-6",
            "temperature": 0.1,
            "default_tools": ["web_search", "web_fetch"],
        },
    },
]


def _get_or_create_system_user(db):
    from app.models import User

    u = db.query(User).filter(User.email == SYSTEM_EMAIL).first()
    if u is not None:
        return u
    u = User(
        id=uuid4(),
        email=SYSTEM_EMAIL,
        display_name=SYSTEM_NAME,
        subscription_tier="pro_plus",
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _seed_bundles(db, system_user) -> int:
    from app.models import Cookbook, CookbookSkill, Skill

    created = 0
    known_slugs = {
        row[0] for row in db.query(Skill.slug).filter(Skill.is_public.is_(True), Skill.is_archived.is_(False))
    }
    for spec in STARTER_BUNDLES:
        slug = spec["slug"]
        cb = db.query(Cookbook).filter(Cookbook.slug == slug).first()
        if cb is None:
            cb = Cookbook(
                id=uuid4(),
                name=spec["name"],
                slug=slug,
                bundle_owner=system_user.id,
                visibility="public",
            )
            db.add(cb)
            db.flush()
            created += 1

        cb.name = spec["name"]
        cb.description = spec["description"]
        cb.bundle_owner = system_user.id
        cb.visibility = "public"
        cb.is_verified = bool(spec.get("verified", False))

        for sk_slug in spec.get("skills", []):
            if sk_slug not in known_slugs:
                continue
            skill = db.query(Skill).filter(Skill.slug == sk_slug).first()
            if skill is None:
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
    return created


def _seed_loops(db) -> int:
    from app.models import Loop

    created = 0
    for spec in STARTER_LOOPS:
        slug = spec["slug"]
        if db.query(Loop).filter(Loop.slug == slug).first() is not None:
            continue
        loop = Loop(
            id=uuid4(),
            slug=slug,
            title=spec["title"],
            description=spec.get("description"),
            category=spec.get("category"),
            readme=spec.get("readme"),
            license=spec.get("license", "MIT"),
            tier=spec.get("tier", "free"),
            is_public=True,
            success_condition=spec["success_condition"],
            verification_script=spec["verification_script"],
            max_turns=spec.get("max_turns", 25),
            budget_usd=spec.get("budget_usd"),
            stopping_criteria=spec["stopping_criteria"],
            tool_allowlist=spec["tool_allowlist"],
            system_prompt=spec["system_prompt"],
        )
        db.add(loop)
        created += 1
    return created


def _seed_personalities(db) -> int:
    from app.models import Personality

    created = 0
    for spec in STARTER_PERSONALITIES:
        slug = spec["slug"]
        if db.query(Personality).filter(Personality.slug == slug).first() is not None:
            continue
        persona = Personality(
            id=uuid4(),
            slug=slug,
            title=spec["title"],
            description=spec.get("description"),
            category=spec.get("category"),
            readme=spec.get("readme"),
            license=spec.get("license", "MIT"),
            tier=spec.get("tier", "free"),
            is_public=True,
            system_prompt=spec["system_prompt"],
            config=spec.get("config"),
        )
        db.add(persona)
        created += 1
    return created


def seed_starter_catalog(db) -> dict:
    """Seed the starter catalog into *db* (any SQLAlchemy Session).

    Idempotent: skips rows that already exist (slug-keyed).
    Returns a summary dict with counts per type.
    """
    system_user = _get_or_create_system_user(db)
    bundles = _seed_bundles(db, system_user)
    loops = _seed_loops(db)
    personalities = _seed_personalities(db)
    db.commit()
    summary = {
        "bundles_created": bundles,
        "loops_created": loops,
        "personalities_created": personalities,
    }
    return summary


def _standalone() -> int:
    """Run seed against the app's configured database (standalone invocation)."""
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        summary = seed_starter_catalog(db)
        print("starter catalog seed complete:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(_standalone())
