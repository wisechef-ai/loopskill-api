"""Everything that must happen to the rest of the system after a version lands.

Publishing a ``SkillVersion`` is a desired-state change for every bundle that
declares the skill, and each of those bundles reaches its members through a
different mechanism. This module owns that propagation as one unit so the
publish route stays a route:

  1. generation token — bump ``Bundle.updated_at`` on the declaring bundles, or
     the reconcile 304 fast-path hides the new version from polling agents
     forever (activate_0701 Phase 0, a live-prod fix).
  2. bundle lock — re-mint the declaring bundles' locks, or reconcile keeps
     serving a lock that still freezes the old version (converge_0208 P1).
  3. search index — BM25 reindex so the new content is findable.
  4. live fan-out — notify subscribed bundles (Phase D).

Steps 1 and 2 are load-bearing for correctness and share the publish
transaction. Steps 3 and 4 are best-effort: a search or fan-out failure must
not fail a publish whose bytes are already durably on disk.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import BundleSkill, Skill

logger = logging.getLogger(__name__)


def propagate_desired_state(db: Session, skill: Skill) -> None:
    """Make the new head visible to every bundle that declares ``skill``.

    Commits: the generation bump and the lock re-mint describe the same event
    and must land together.
    """
    from app.services.bundle_lock_sync import resync_locks_for_skill
    from app.services.reconcile import bump_declaring_bundles

    bump_declaring_bundles(db, skill.id)

    # A publish moves the head, so every 'track' entry pointing at this skill
    # now resolves somewhere new. Best-effort per bundle by design: a publisher
    # must never be blocked by an unresolvable entry in a bundle they do not
    # own — those bundles keep their previous, valid lock revision.
    resync_locks_for_skill(db, skill.id)
    db.commit()


def reindex_search(db: Session, skill: Skill) -> None:
    """BM25 reindex. Non-critical at publish time — log and continue."""
    try:
        from app.search_index import reindex_bm25

        reindex_bm25(skill.slug, db)
    # Rationale: BM25 reindex is non-critical at publish time; failure → log and continue
    except Exception:  # noqa: BLE001
        logger.exception("BM25 reindex failed for %s (non-fatal)", skill.slug)


async def fan_out_version_published(db: Session, skill: Skill, semver: str) -> None:
    """Notify every bundle that declares this skill (Phase D live-sync).

    On Postgres this goes via pg_notify so all processes receive it; on SQLite
    tests it publishes directly to the in-process subscribers. Non-critical —
    any error is logged and swallowed.
    """
    from app.sync_fanout import emit_cookbook_event

    try:
        cookbook_ids = [
            str(cs.bundle_id)
            for cs in db.query(BundleSkill)
            .filter(BundleSkill.skill_id == skill.id, BundleSkill.source != "disabled")
            .all()
        ]
        if cookbook_ids:
            await emit_cookbook_event(
                db,
                cookbook_ids,
                {
                    "slug": skill.slug,
                    "version": semver,
                    "action": "version_published",
                    "skill_id": str(skill.id),
                },
            )
            db.commit()
    # Rationale: fanout is non-critical at publish time; any error → log and continue
    except Exception:  # noqa: BLE001
        logger.exception("phase-D fan-out failed for %s@%s (non-fatal)", skill.slug, semver)
