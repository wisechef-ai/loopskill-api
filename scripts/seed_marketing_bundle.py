#!/usr/bin/env python3
"""Seed the "Corey's Marketing" bundle from the github-marketing tap.

Composes ONE curated public bundle ("Marketing") owned by the WiseChef Editorial
SYSTEM account, holding every skill from Corey Haines' MIT-licensed pack
(``coreyhaines31/marketingskills``, tap source id ``github-marketing``).

WHY a dedicated seed (vs the editorial-cookbook seed): those attach INTERNAL
public skills by slug. These 47 are FEDERATED — they are never published through
our pipeline and have no internal Skill row until materialized. So this seed:

  1. Walks the live tap (``github_tap_fetch('github-marketing')``) to discover
     the real skill dirs — never a hard-coded list, so it tracks upstream as the
     repo adds/removes skills (Adam decision 2026-07-12: live, no SHA pinning).
  2. Materializes each as a thin PRIVATE pointer row via
     ``bundle_external.materialize_external_skill`` (no rehosting; the row is a
     re-resolution descriptor + a scan-on-add trust badge).
  3. Attaches each to the bundle with source='custom-added'.

DESIGN INVARIANTS (mirror seed_editorial_cookbooks.py):
  - NEVER touch the is_base=true 'WiseChef Recipes Catalog'.
  - Bundle owner is the editorial SYSTEM user (never owner-less — the
    ck_cookbooks_owner_required CHECK fires on flush).
  - Idempotent: re-running upserts by bundle slug + membership (no dupes). Only
    tap-resolved skills are attached; an unresolvable skill is reported, never
    fabricated.
  - MIT ATTRIBUTION preserved: the copyright line rides in the bundle description
    AND each materialized row carries license='MIT' (resolved by the tap). This
    is the MIT redistribution requirement — do not strip it.

Live-fetch cost: materialize fetches each skill's origin body ONCE (scan-on-add).
For ~47 skills that is ~47 GitHub raw fetches at SEED time (not per request).
Transient failures are skipped + reported, so a partial GitHub outage yields a
partial bundle you can top up by re-running — never a crash, never a fabricated
membership.

Run on prod:
    cd /home/wisechef/loopskill-api && ./venv/bin/python scripts/seed_marketing_bundle.py
Add --dry-run to preview without writing.
"""

from __future__ import annotations

import sys
from uuid import uuid4

TAP_SOURCE = "github-marketing"
BUNDLE_SLUG = "coreys-marketing"
BUNDLE_NAME = "Corey's Marketing"
BUNDLE_DESC = (
    "The complete marketing operating system for your agent — 40+ conversion, "
    "SEO, copywriting, paid, growth, and RevOps skills that work together. "
    "Ask your agent to optimize a landing page, write a cold-email sequence, "
    "audit your SEO, or plan a launch, and it applies the right framework.\n\n"
    "Skills by Corey Haines (coreyhaines31/marketingskills), MIT licensed. "
    "Copyright (c) 2025 Corey Haines. Surfaced live from origin — never rehosted."
)

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


def _discover_tap_skill_slugs() -> list[str]:
    """Walk the live tap and return every resolved external skill slug.

    Slugs are the adapter's namespaced form ``github-marketing--<name>``. Empty
    list on a full tap outage (caller reports it, never fabricates).
    """
    from app.services.github_taps_live import github_tap_fetch

    rows = github_tap_fetch(TAP_SOURCE)("")  # empty query → all skills
    slugs = [str(r["slug"]) for r in rows if r.get("slug")]
    return sorted(set(slugs))


def seed(dry_run: bool = False, allow_partial: bool = False) -> int:
    from app.database import SessionLocal
    from app.models import Bundle, BundleSkill, Skill
    from app.models import User as UserModel
    from app.services.bundle_external import descriptor_source_slug, materialize_external_skill

    db = SessionLocal()
    attached, skipped, unresolved = 0, 0, []
    try:
        system = _get_or_create_system_user(db, UserModel)

        tap_slugs = _discover_tap_skill_slugs()
        if not tap_slugs:
            print(
                f"ABORT: tap '{TAP_SOURCE}' resolved 0 skills (GitHub outage / "
                "rate-limit / repo moved). Nothing written.",
                file=sys.stderr,
            )
            return 1
        print(f"tap '{TAP_SOURCE}' resolved {len(tap_slugs)} skills")

        # Upsert the bundle by slug.
        cb = db.query(Bundle).filter(Bundle.slug == BUNDLE_SLUG).first()
        is_new = cb is None
        if is_new:
            # Owner MUST be set at construction — ck_cookbooks_owner_required
            # (is_base OR owner NOT NULL) fires on flush.
            cb = Bundle(
                id=uuid4(),
                name=BUNDLE_NAME,
                slug=BUNDLE_SLUG,
                bundle_owner=system.id,
                visibility="public",
            )
            db.add(cb)
            db.flush()
        # Defensive: never convert the sacrosanct base catalog.
        if cb.is_base:
            print(f"REFUSING to mutate is_base bundle for slug={BUNDLE_SLUG}", file=sys.stderr)
            return 1
        cb.name = BUNDLE_NAME
        cb.description = BUNDLE_DESC
        cb.bundle_owner = system.id
        cb.visibility = "public"
        # is_verified is set AFTER the attach loop — only a COMPLETE seed earns
        # the verified badge.

        current_slugs = set(tap_slugs)
        for ext_slug in tap_slugs:
            # Materialize the federated pointer row (idempotent by ext:source:slug).
            skill = materialize_external_skill(db, TAP_SOURCE, ext_slug)
            if skill is None:
                unresolved.append(ext_slug)
                continue
            exists = (
                db.query(BundleSkill)
                .filter(
                    BundleSkill.bundle_id == cb.id,
                    BundleSkill.skill_id == skill.id,
                )
                .first()
            )
            if exists is None:
                db.add(
                    BundleSkill(
                        bundle_id=cb.id,
                        skill_id=skill.id,
                        source="custom-added",
                    )
                )
                attached += 1
            elif exists.source == "disabled":
                # Re-enable a member we previously reconciled-off but that has
                # returned upstream (removal is not permanent).
                exists.source = "custom-added"
                attached += 1
            else:
                skipped += 1

        # Removal reconciliation. A member whose external slug is no longer in
        # the live tap walk points at content that may now 404. Disable it (soft,
        # reversible) so the public bundle never advertises
        # a dead skill. We only reconcile OUR tap's members, and only when the
        # walk is trustworthy (non-empty — the empty case aborted earlier).
        reconciled_off = 0
        members = (
            db.query(BundleSkill)
            .join(Skill, Skill.id == BundleSkill.skill_id)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .all()
        )
        for m in members:
            sk = db.query(Skill).filter(Skill.id == m.skill_id).first()
            pair = descriptor_source_slug(sk) if sk is not None else None
            if pair is None or pair[0] != TAP_SOURCE:
                continue  # not one of our tap's skills — leave it alone
            if pair[1] not in current_slugs:
                m.source = "disabled"
                reconciled_off += 1

        # Partial-failure honesty. Transient origin/scan failures leave skills
        # unresolved; a "verified" public bundle must be COMPLETE, not silently
        # missing members. Refuse to verify + fail nonzero unless the caller
        # explicitly accepts a partial seed.
        complete = not unresolved
        cb.is_verified = complete

        if dry_run:
            print(
                f"[dry-run] bundle {'CREATE' if is_new else 'UPDATE'} slug={BUNDLE_SLUG} "
                f"would attach={attached} already_present={skipped} "
                f"reconcile_off={reconciled_off} verified={complete}"
            )
            if unresolved:
                print(f"[dry-run] UNRESOLVED (skipped, not fabricated): {unresolved}")
            db.rollback()
            return 0 if (complete or allow_partial) else 1

        if unresolved and not allow_partial:
            print(
                f"ABORT: {len(unresolved)} skill(s) failed to resolve "
                f"(transient origin/scan failure?): {unresolved}. "
                "No write — re-run when origin is healthy, or pass --allow-partial "
                "to seed the resolvable subset (bundle will NOT be marked verified).",
                file=sys.stderr,
            )
            db.rollback()
            return 1

        db.commit()
        print(
            f"seed complete: bundle={BUNDLE_SLUG} {'created' if is_new else 'updated'} "
            f"attached={attached} already_present={skipped} reconcile_off={reconciled_off} "
            f"verified={complete} owner={system.email}"
        )
        if unresolved:
            print(
                f"WARNING — PARTIAL seed ({len(unresolved)} unresolved, --allow-partial): "
                f"{unresolved}. Bundle is NOT marked verified until a clean re-run."
            )
        # Verification read-back.
        n = (
            db.query(BundleSkill)
            .filter(BundleSkill.bundle_id == cb.id, BundleSkill.source != "disabled")
            .count()
        )
        print(f"verify: '{BUNDLE_NAME}' bundle holds {n} active skills, visibility={cb.visibility}")
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
