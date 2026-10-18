#!/usr/bin/env python3
"""qa1018 C03 — remove unservable members from the public SEO bundle (prod data fix).

Persona QA on prod (2026-10-18): ``seo-and-search-growth`` advertised "every
free skill in this bundle installs verbatim", yet 4 of its 8 members served no
skill over the public bundle surface, verified live the same day:

  * ``alirezarezvani ... ai-seo``          — row gone from the federated hub
    index (unresolvable) and its origin path 404s; also a second ``ai-seo``.
  * ``sickn33 ... programmatic-seo``       — row gone from the federated hub
    index (unresolvable); also a second ``programmatic-seo``.
  * ``kostja94 ... local-seo``             — resolves, but the origin fetcher
    returns nothing for it, so the public surface serves a placeholder.
  * ``aaron-he-zhu ... on-page-seo-auditor`` — origin now serves a "has moved"
    signpost, not a skill.

Its description also carried an internal note ("the 'seo' / 'seo audit'
searches LoopSkill kept coming up empty for ... plenty of headroom to grow
this bundle later").

Source of truth: ``THEMES`` in ``scripts/seed_themed_public_bundles_0811.py``
(fixed in the same change). That seed only ever ADDS members and is not re-run
on deploy, so the live rows need this one-off, tracked, idempotent fix.

What it does, per bundle in ``FIXES`` — nothing else is touched:
  * sets each dropped member's ``bundle_skills.source`` to ``'disabled'``
    (the existing reversible removal state every public surface already
    filters out). No row is deleted; shared ``skills`` rows are not modified.
  * replaces the bundle description with buyer-facing copy;
  * bumps ``bundles.updated_at`` so conditional-GET / reconcile caches see it;
  * reads back the active member set and description and exits 1 on mismatch.

``agent-marketing`` (a personal bundle) is listed too but is only touched
with ``--include-owner-bundles``.

Run on the host with the app venv (dry-run first)::

    cd /home/wisechef/loopskill-api
    sudo -u wisechef env PYTHONPATH=. ./venv/bin/python scripts/fix_qa1018_seo_bundle.py --dry-run
    sudo -u wisechef env PYTHONPATH=. ./venv/bin/python scripts/fix_qa1018_seo_bundle.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

_EXT = "ext:hermes-hub:"
_SKS = "ext:skills-sh:"

SEO_DESCRIPTION = (
    "SEO skills for your agent: audit a website or an online store, get your "
    "pages cited in AI search answers, and build SEO pages at scale from "
    "templates and data. Each skill is fetched from its author's public repo "
    "when you install it."
)

FIXES: dict[str, dict] = {
    "seo-and-search-growth": {
        "description": SEO_DESCRIPTION,
        # Members that must remain active afterwards (verified servable live).
        "keep": [
            _EXT + "skills-sh-coreyhaines31-marketingskills-ai-seo",
            _EXT + "skills-sh-affilino-ecommerce-seo-audit-skill-ecommerce-seo-audit",
            _EXT + "skills-sh-firecrawl-firecrawl-workflows-firecrawl-seo-audit",
            _EXT + "skills-sh-kostja94-marketing-skills-programmatic-seo",
        ],
        "drop": {
            _EXT
            + "skills-sh-alirezarezvani-claude-skills-ai-seo": "unresolvable at origin; duplicate ai-seo",
            _EXT
            + "skills-sh-aaron-he-zhu-seo-geo-claude-skills-on-page-seo-auditor": "origin serves a 'has moved' signpost",
            _EXT
            + "skills-sh-sickn33-antigravity-awesome-skills-programmatic-seo": "unresolvable at origin; duplicate programmatic-seo",
            _EXT + "skills-sh-kostja94-marketing-skills-local-seo": "origin fetch returns no body",
        },
    },
    # A PERSONAL bundle (owned by a human account, not the editorial user):
    # 11 of 25 members serve the placeholder because their upstream path no
    # longer exists (e.g. coreyhaines31/marketingskills skills/page-cro -> 404,
    # 2026-10-18). Only applied with --include-owner-bundles: removing members
    # from a person's own bundle is the owner's call. Description left as is.
    "agent-marketing": {
        "owner_bundle": True,
        "description": None,
        "keep": [
            _SKS + "coreyhaines31--marketingskills--directory-submissions",
            _SKS + "samuraigpt--generative-media-skills--muapi-youtube-thumbnail",
            _SKS + "claude-office-skills--skills--tiktok-marketing",
            _SKS + "onewave-ai--claude-skills--landing-page-copywriter",
            _SKS + "intellectronica--agent-skills--youtube-transcript",
            _SKS + "coreyhaines31--marketingskills--seo-audit",
            _SKS + "coreyhaines31--marketingskills--copywriting",
            _SKS + "coreyhaines31--marketingskills--content-strategy",
            _SKS + "coreyhaines31--marketingskills--marketing-psychology",
            _SKS + "coreyhaines31--marketingskills--ad-creative",
            _SKS + "coreyhaines31--marketingskills--cold-email",
            _SKS + "inference-sh--skills--youtube-thumbnail-design",
            _SKS + "charlie947--social-media-skills--youtube-thumbnail",
            _SKS + "anthropics--skills--brand-guidelines",
        ],
        "drop": {
            _SKS + "coreyhaines31--marketingskills--competitor-alternatives": "origin fetch returns no body",
            _SKS + "coreyhaines31--marketingskills--analytics-tracking": "origin fetch returns no body",
            _SKS + "coreyhaines31--marketingskills--page-cro": "origin fetch returns no body",
            _SKS + "coreyhaines31--marketingskills--email-sequence": "origin fetch returns no body",
            _SKS + "coreyhaines31--marketingskills--referral-program": "origin fetch returns no body",
            _SKS + "coreyhaines31--marketingskills--launch-strategy": "origin fetch returns no body",
            _SKS + "coreyhaines31--marketingskills--schema-markup": "origin fetch returns no body",
            _SKS + "bradautomates--head-of-content--tiktok-research": "origin fetch returns no body",
            _SKS + "kostja94--marketing-skills--tiktok-captions": "origin fetch returns no body",
            _SKS + "kostja94--marketing-skills--linkedin-posts": "origin fetch returns no body",
            _SKS + "bradautomates--head-of-content--instagram-research": "origin fetch returns no body",
        },
    },
}


def _selected(include_owner_bundles: bool):
    for slug, fix in FIXES.items():
        if fix.get("owner_bundle") and not include_owner_bundles:
            print(f"{slug}: skipped (personal bundle; pass --include-owner-bundles)")
            continue
        yield slug, fix


def _active_slugs(db, bundle) -> list[str]:
    from app.models import BundleSkill, Skill

    rows = (
        db.query(Skill.slug)
        .join(BundleSkill, BundleSkill.skill_id == Skill.id)
        .filter(BundleSkill.bundle_id == bundle.id, BundleSkill.source != "disabled")
        .all()
    )
    return sorted(r[0] for r in rows)


def apply(dry_run: bool = True, db=None, include_owner_bundles: bool = False) -> int:
    """Plan (and unless ``dry_run``, apply + verify) every fix. Returns an exit code."""
    from app.models import Bundle, BundleSkill, Skill

    own_session = db is None
    if own_session:
        from app.database import SessionLocal

        db = SessionLocal()
    rc = 0
    selected = list(_selected(include_owner_bundles))
    try:
        for slug, fix in selected:
            cb = db.query(Bundle).filter(Bundle.slug == slug).first()
            if cb is None:
                print(f"{slug}: bundle not found — nothing to do")
                continue
            if cb.is_base:
                print(f"{slug}: REFUSING to touch an is_base bundle", file=sys.stderr)
                rc = 1
                continue
            before = _active_slugs(db, cb)
            print(f"{slug}: active before ({len(before)}): {before}")
            changes = 0
            for skill_slug, reason in fix["drop"].items():
                row = (
                    db.query(BundleSkill)
                    .join(Skill, Skill.id == BundleSkill.skill_id)
                    .filter(BundleSkill.bundle_id == cb.id, Skill.slug == skill_slug)
                    .first()
                )
                if row is None or row.source == "disabled":
                    print(f"  = {skill_slug}: already absent/disabled")
                    continue
                print(f"  - disable {skill_slug} (was {row.source!r}): {reason}")
                row.source = "disabled"
                changes += 1
            if fix["description"] is not None and (cb.description or "") != fix["description"]:
                print(f"  ~ description: {cb.description!r}\n    -> {fix['description']!r}")
                cb.description = fix["description"]
                changes += 1
            missing = sorted(set(fix["keep"]) - set(before))
            if missing:
                print(f"  ! expected-kept members not active: {missing}", file=sys.stderr)
                rc = 1
            if changes:
                cb.updated_at = datetime.now(timezone.utc)
            print(f"{slug}: {changes} change(s) planned")

        if dry_run or rc:
            db.rollback()
            print(
                "[dry-run] rolled back" if dry_run else "ABORT: rolled back",
                file=sys.stderr if rc else sys.stdout,
            )
            return rc

        db.commit()
        for slug, fix in selected:
            cb = db.query(Bundle).filter(Bundle.slug == slug).first()
            if cb is None:
                continue
            after = _active_slugs(db, cb)
            ok = after == sorted(fix["keep"]) and cb.description == fix["description"]
            print(
                f"verify {slug}: active={len(after)} {after} description_ok={cb.description == fix['description']}"
            )
            if not ok:
                print(f"VERIFY FAILED for {slug}", file=sys.stderr)
                rc = 1
        return rc
    finally:
        if own_session:
            db.close()


if __name__ == "__main__":
    sys.exit(
        apply(
            dry_run="--dry-run" in sys.argv,
            include_owner_bundles="--include-owner-bundles" in sys.argv,
        )
    )
