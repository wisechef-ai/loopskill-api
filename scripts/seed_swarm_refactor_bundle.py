#!/usr/bin/env python3
"""Seed the public ``swarm-refactor-campaign`` bundle (coldstart_0609, clause #9).

The reproducible artifact behind the "run Teknium's method on YOUR repo"
story: one bundle a fresh agent installs with a single command,

    hermes skills install well-known:https://app.loopskill.io/api/bundles/public/swarm-refactor-campaign

Membership is a SNAPSHOT (Adam's default for curated bundles): a fixed list,
no reconciliation cron. Skills stay fresh because internal ones are served
from the catalog and the one federated member (botmaker) is live-fetched
from origin through its tap pointer.

Members (real slugs only — a missing slug is reported, never fabricated):
  hermes-swarm-refactor-campaign  the runbook (gate stack, lane shape, knobs)
  musk-5-step-algorithm           deletion-first discipline every lane opens with
  botmaker                        federated: mint the specialist bots (github-botmaker tap)

Usage:
  PYTHONPATH=$PWD python scripts/seed_swarm_refactor_bundle.py --dry-run
  PYTHONPATH=$PWD python scripts/seed_swarm_refactor_bundle.py

Idempotent: re-running attaches 0 and reports already_present=N.
"""

from __future__ import annotations

import sys
from uuid import uuid4

BUNDLE_SLUG = "swarm-refactor-campaign"
BUNDLE_NAME = "Swarm Refactor Campaign"
BUNDLE_DESC = (
    "Run a Teknium-style autonomous refactor swarm on your own repo — gate stack first "
    "(frozen-surface parity, identical-red-set baseline, AST differentials), then nested "
    "Hermes subagents in git worktrees. Includes the runbook, the deletion-first discipline, "
    "and botmaker to mint the specialist bots. Reverse-engineered from hermes-agent PR #102117. "
    "botmaker by techjanitor (techjanitor/botmaker), MIT."
)
INTERNAL_SKILLS = ["hermes-swarm-refactor-campaign", "musk-5-step-algorithm"]
FEDERATED = [("github-botmaker", "github-botmaker--botmaker")]
SYSTEM_EMAIL = "editorial@wisechef.ai"
SYSTEM_NAME = "LoopSkill Editorial"


def _get_or_create_system_user(db, User):
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
    from app.models import Bundle, BundleSkill, Skill, User
    from app.services.bundle_external import materialize_external_skill

    db = SessionLocal()
    attached, already, missing = 0, 0, []
    try:
        system = _get_or_create_system_user(db, User)
        members: list = []
        for slug in INTERNAL_SKILLS:
            s = (
                db.query(Skill)
                .filter(Skill.slug == slug, Skill.is_public.is_(True), Skill.is_archived.is_(False))
                .first()
            )
            (members.append(s) if s is not None else missing.append(slug))
        for source, ext_slug in FEDERATED:
            s = materialize_external_skill(db, source, ext_slug)
            (members.append(s) if s is not None else missing.append(f"{source}:{ext_slug}"))
        if missing:
            # A partial "verified" bundle is a lie; abort loud, write nothing.
            print(f"ABORT: unresolved members {missing} — nothing written", file=sys.stderr)
            return 1

        cb = db.query(Bundle).filter(Bundle.slug == BUNDLE_SLUG).first()
        if cb is None:
            cb = Bundle(
                id=uuid4(), name=BUNDLE_NAME, slug=BUNDLE_SLUG, bundle_owner=system.id, visibility="public"
            )
            db.add(cb)
            db.flush()
        if cb.is_base:
            print(f"REFUSING to mutate is_base bundle {BUNDLE_SLUG}", file=sys.stderr)
            return 1
        cb.name, cb.description, cb.bundle_owner, cb.visibility = (
            BUNDLE_NAME,
            BUNDLE_DESC,
            system.id,
            "public",
        )

        for s in members:
            exists = (
                db.query(BundleSkill)
                .filter(BundleSkill.bundle_id == cb.id, BundleSkill.skill_id == s.id)
                .first()
            )
            if exists is None:
                db.add(BundleSkill(bundle_id=cb.id, skill_id=s.id, source="custom-added"))
                attached += 1
            else:
                already += 1
        cb.is_verified = True  # only reached when every member resolved

        print(
            f"bundle={BUNDLE_SLUG} members={len(members)} attached={attached} already_present={already} dry_run={dry_run}"
        )
        if dry_run:
            db.rollback()
        else:
            db.commit()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(seed(dry_run="--dry-run" in sys.argv))
