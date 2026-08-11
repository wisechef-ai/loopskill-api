#!/usr/bin/env python3
"""bundles0811-P1-follow-seed gate 2 — seed 5 themed public bundles (closes #67).

Plan step 5, verbatim: "Seeding is a first-class step, not an afterthought.
Publish 5 themed public bundles assembled from the EXISTING federated index —
NO NEW SKILLS AUTHORED. They exist to prove a bundle is cheap to make and
worth following."

WHERE THE SKILLS COME FROM — the EXISTING federated index, never new content:
  Every skill below is a real row in ``federation_hub_skills`` (90,605 rows,
  probed live 2026-08-11) with ``install_path='fetch_origin'`` AND non-empty
  ``repo``/``path`` — the 20,509-row resolvable subset P3 measured at 88%
  direct-resolve (the other ~10% recover via P3's bounded tree-walk fallback,
  ``app.services.federation_hub_install``). No slug here was invented; every
  one was selected by querying the live prod table (see the PR body for the
  exact SQL). ``source='hermes-hub'`` is the wired adapter
  (``app.services.federation_adapters.HermesHubAdapter``) whose ``resolve()``
  reads these exact rows by slug — so materialize below hits the SAME table
  this script queried, not a re-derived guess.

WHY THESE 5 THEMES — driven by ``missing_skill_queries`` (32 rows of real
zero-result searches, probed 2026-08-11), not picked cold:
  1. Copywriting & Content Humanizer — "copywriting" (7 query hits across
     case variants) + "humanizer" (4 hits) are the single most repeated
     failed searches in the table.
  2. Cold Outreach & Email Sequences — "email composer"/"email-composer"
     appear as distinct failed searches; a marketing-adjacent gap sitting
     right next to #1.
  3. Terraform & Kubernetes Infra Ops — "terraform" (2 hits) plus
     "kubernetes" (implied by the same devops-search cluster); the
     catalog's infra-ops gap.
  4. Proactive & Autonomous Agent Ops — "proactive"/"proac"/"proa"/"proact"/
     "proacti"/"proactiv"/"proactive agent" is SEVEN separate partial-typing
     variants of one query in the table (someone typing it out letter by
     letter, live, and getting nothing every time) + "agentic-os" (2 hits) +
     "loopskill" itself (4 hits) — the loudest unserved-demand signal in the
     table by a wide margin.
  5. SEO & Search Growth — "seo" + "seo audit" hits, and the largest
     resolvable federated cluster of any keyword probed (203
     ``fetch_origin``-eligible rows) — plenty of headroom to grow this
     bundle later without re-querying from scratch.

THE F1 BUG THIS SCRIPT'S EXISTENCE SURFACED (fixed in the same PR,
``app/bundle_wellknown_routes.py``): every materialized external skill was
built with ``tier='external'`` — a value ``bundle_wellknown_routes._is_free``
never treated as free — so the well-known index flagged EVERY federated
member ``locked`` and ``install.sh`` installed 0 skills from ANY
federated-only bundle (reproduced live against the pre-existing
``coreys-marketing`` bundle: 49/49 locked, 0 installed). Verify this script's
bundles AFTER that fix lands, or every one of them will look "seeded" while
installing nothing — see the PR body for the pasted ``curl | bash`` proof.

DESIGN INVARIANTS (mirrors seed_editorial_cookbooks.py / seed_marketing_bundle.py):
  - NEVER touch the is_base=true base catalog.
  - Bundle owner is the editorial SYSTEM user (never owner-less — the
    ck_cookbooks_owner_required CHECK fires on flush).
  - Every seeded bundle gets an explicit ``slug`` at construction — P1 made
    public-with-null-slug structurally impossible
    (``Bundle._validate_visibility``), so this is belt-and-suspenders, not
    load-bearing, but stated for the reader.
  - Idempotent: re-running upserts by bundle slug + BundleSkill membership
    (``materialize_external_skill`` is itself idempotent by
    ``ext:hermes-hub:<slug>``). Re-running does not create duplicate rows.
  - An unresolvable federated slug is reported, never fabricated — a bundle
    with any unresolved member is NOT marked ``is_verified`` unless
    ``--allow-partial`` is passed.

Run on prod:
    cd /home/wisechef/loopskill-api && ./venv/bin/python scripts/seed_themed_public_bundles_0811.py
Add --dry-run to preview without writing. Add --allow-partial to accept a
seed with some unresolvable federated rows (transient origin outage etc).
"""

from __future__ import annotations

import sys
from uuid import uuid4

SYSTEM_EMAIL = "editorial@wisechef.ai"
SYSTEM_NAME = "WiseChef Editorial"
FEDERATION_SOURCE = "hermes-hub"

# ── Theme definitions ───────────────────────────────────────────────────────
# ``skills`` are ``federation_hub_skills.slug`` values (the FederationHubSkill
# table's own slug column — NOT re-derived, NOT invented). Queried live
# 2026-08-11 with: install_path='fetch_origin' AND repo IS NOT NULL AND
# repo != '' AND path IS NOT NULL AND path != '' (the 20,509-row resolvable
# set) filtered per theme keyword, excluding the three banned legacy tier
# words this repo's discipline test forbids anywhere in prose.
THEMES: list[dict] = [
    {
        "slug": "copywriting-and-humanizer",
        "name": "Copywriting & Content Humanizer",
        "description": (
            "Write copy that converts, then make it sound human. The most "
            "repeated zero-result search on LoopSkill was 'copywriting' — "
            "this bundle is the direct answer: conversion copy, ad copy, "
            "and AI-text humanizers, assembled from the open federated "
            "skill index. Every member is fetched live from its own public "
            "repo at install time — nothing here is rehosted."
        ),
        "skills": [
            "skills-sh-coreyhaines31-marketingskills-copywriting",
            "skills-sh-alirezarezvani-claude-skills-copywriting",
            "skills-sh-davila7-claude-code-templates-copywriting",
            "skills-sh-guia-matthieu-clawfu-skills-conversion-copywriting",
            "skills-sh-claude-office-skills-skills-ads-copywriter",
            "skills-sh-alirezarezvani-claude-skills-content-humanizer",
            "skills-sh-humanizerai-agent-skills-humanize",
            "skills-sh-softaworks-agent-toolkit-humanizer",
        ],
    },
    {
        "slug": "cold-outreach-and-email",
        "name": "Cold Outreach & Email Sequences",
        "description": (
            "Write the emails that get replies. Cold-email openers, "
            "multi-step sequences, and a composer agents can hand a full "
            "campaign to — the exact catalog gap 'email composer' searches "
            "kept turning up empty on LoopSkill. Sourced from the open "
            "federated skill index; fetched live from origin on install."
        ),
        "skills": [
            "skills-sh-coreyhaines31-marketingskills-cold-email",
            "skills-sh-alirezarezvani-claude-skills-cold-email",
            "skills-sh-onewave-ai-claude-skills-cold-email-sequence-generator",
            "skills-sh-paramchoudhary-resumeskills-cold-email-writer",
            "skills-sh-davila7-claude-code-templates-email-composer",
            "skills-sh-coreyhaines31-marketingskills-email-sequence",
            "skills-sh-anthropics-knowledge-work-plugins-email-sequence",
            "skills-sh-alirezarezvani-claude-skills-email-sequence",
        ],
    },
    {
        "slug": "terraform-and-kubernetes-ops",
        "name": "Terraform & Kubernetes Infra Ops",
        "description": (
            "Provision and run infrastructure without leaving the agent "
            "loop. Terraform module generation and review paired with "
            "Kubernetes manifest and debug skills — the infra-ops gap "
            "'terraform' searches on LoopSkill kept finding nothing for. "
            "Sourced from the open federated skill index; fetched live "
            "from origin on install."
        ),
        "skills": [
            "skills-sh-mindrally-skills-terraform",
            "skills-sh-akin-ozer-cc-devops-skills-terraform-generator",
            "skills-sh-alirezarezvani-claude-skills-terraform-patterns",
            "skills-sh-antonbabenko-terraform-skill-terraform-skill",
            "skills-sh-mindrally-skills-kubernetes",
            "skills-sh-bobmatnyc-claude-mpm-skills-kubernetes",
            "skills-sh-akin-ozer-cc-devops-skills-k8s-debug",
            "skills-sh-wshobson-agents-k8s-manifest-generator",
        ],
    },
    {
        "slug": "proactive-and-autonomous-agent-ops",
        "name": "Proactive & Autonomous Agent Ops",
        "description": (
            "Agents that act before you ask, and improve on their own "
            "runs. The single loudest unserved-demand signal in "
            "LoopSkill's search log — 'proactive' was searched, "
            "abandoned, and re-typed letter by letter seven separate times "
            "with zero results every time. This bundle is the direct "
            "answer: proactive-agent patterns, self-improving-agent loops, "
            "and autonomous-agent harnesses from the open federated "
            "index, fetched live from origin on install."
        ),
        "skills": [
            "skills-sh-halthelobster-proactive-agent-proactive-agent",
            "skills-sh-sundial-org-awesome-openclaw-skills-proactive-agent",
            "skills-sh-yanhongxi-openclaw-proactive-self-improving-agent-proactive-self-improving-agent",
            "skills-sh-charon-fan-agent-playbook-self-improving-agent",
            "skills-sh-alirezarezvani-claude-skills-self-improving-agent",
            "skills-sh-affaan-m-ecc-autonomous-agent-harness",
            "skills-sh-sickn33-antigravity-awesome-skills-autonomous-agent-patterns",
            "skills-sh-davila7-claude-code-templates-autonomous-agents",
        ],
    },
    {
        "slug": "seo-and-search-growth",
        "name": "SEO & Search Growth",
        "description": (
            "Grow organic traffic without a marketing team. On-page "
            "audits, programmatic SEO, local SEO, and AI-search "
            "optimization — the 'seo' / 'seo audit' searches LoopSkill "
            "kept coming up empty for, and the single largest resolvable "
            "cluster in the federated index (200+ fetchable SEO skills) — "
            "plenty of headroom to grow this bundle later. Fetched live "
            "from origin on install; nothing rehosted."
        ),
        "skills": [
            "skills-sh-coreyhaines31-marketingskills-ai-seo",
            "skills-sh-alirezarezvani-claude-skills-ai-seo",
            "skills-sh-affilino-ecommerce-seo-audit-skill-ecommerce-seo-audit",
            "skills-sh-firecrawl-firecrawl-workflows-firecrawl-seo-audit",
            "skills-sh-aaron-he-zhu-seo-geo-claude-skills-on-page-seo-auditor",
            "skills-sh-sickn33-antigravity-awesome-skills-programmatic-seo",
            "skills-sh-kostja94-marketing-skills-local-seo",
            "skills-sh-kostja94-marketing-skills-programmatic-seo",
        ],
    },
]


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


def seed(dry_run: bool = False, allow_partial: bool = False) -> int:
    from app.database import SessionLocal
    from app.models import Bundle, BundleSkill
    from app.models import User as UserModel
    from app.services.bundle_external import materialize_external_skill

    db = SessionLocal()
    summary: list[str] = []
    any_hard_failure = False
    try:
        system = _get_or_create_system_user(db, UserModel)

        for theme in THEMES:
            slug = theme["slug"]
            attached = 0
            already_present = 0
            unresolved: list[str] = []

            cb = db.query(Bundle).filter(Bundle.slug == slug).first()
            is_new = cb is None
            if is_new:
                # Owner MUST be set at construction — ck_cookbooks_owner_required
                # (is_base OR owner NOT NULL) fires on flush. slug is set at
                # construction too (belt-and-suspenders alongside the P1
                # validator that would mint one anyway on a public flip).
                cb = Bundle(
                    id=uuid4(),
                    name=theme["name"],
                    slug=slug,
                    bundle_owner=system.id,
                    visibility="public",
                )
                db.add(cb)
                db.flush()
            if cb.is_base:
                print(f"REFUSING to mutate is_base bundle for slug={slug}", file=sys.stderr)
                any_hard_failure = True
                continue

            cb.name = theme["name"]
            cb.description = theme["description"]
            cb.bundle_owner = system.id
            cb.visibility = "public"

            for fed_slug in theme["skills"]:
                skill = materialize_external_skill(db, FEDERATION_SOURCE, fed_slug)
                if skill is None:
                    unresolved.append(fed_slug)
                    continue
                exists = (
                    db.query(BundleSkill)
                    .filter(BundleSkill.bundle_id == cb.id, BundleSkill.skill_id == skill.id)
                    .first()
                )
                if exists is None:
                    db.add(BundleSkill(bundle_id=cb.id, skill_id=skill.id, source="custom-added"))
                    attached += 1
                elif exists.source == "disabled":
                    exists.source = "custom-added"
                    attached += 1
                else:
                    already_present += 1

            complete = not unresolved
            cb.is_verified = complete
            summary.append(
                f"{slug}: {'CREATE' if is_new else 'UPDATE'} attached={attached} "
                f"already_present={already_present} unresolved={len(unresolved)} verified={complete}"
            )
            if unresolved:
                summary.append(f"  UNRESOLVED (skipped, not fabricated): {unresolved}")
                if not allow_partial:
                    any_hard_failure = True

        if dry_run:
            print("[dry-run] " + "\n[dry-run] ".join(summary))
            db.rollback()
            return 0 if not any_hard_failure else 1

        if any_hard_failure and not allow_partial:
            print(
                "ABORT: one or more themes had unresolved federated skills "
                "(transient origin/scan failure?) or an is_base guard tripped. "
                "No write. Re-run when origin is healthy, or pass --allow-partial "
                "to seed the resolvable subset (affected bundles will NOT be "
                "marked verified).",
                file=sys.stderr,
            )
            print("\n".join(summary), file=sys.stderr)
            db.rollback()
            return 1

        db.commit()
        print("seed complete:")
        print("\n".join(summary))

        # Verification read-back.
        for theme in THEMES:
            cb = db.query(Bundle).filter(Bundle.slug == theme["slug"]).first()
            if cb is None:
                continue
            n = (
                db.query(BundleSkill)
                .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
                .count()
            )
            print(
                f"verify: '{cb.name}' (slug={cb.slug}) holds {n} active skills, "
                f"visibility={cb.visibility}, is_verified={cb.is_verified}"
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(
        seed(
            dry_run="--dry-run" in sys.argv,
            allow_partial="--allow-partial" in sys.argv,
        )
    )
