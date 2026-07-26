"""Shared federated-skill title resolution — sp2607fix-1.

Extracted from ``app.library_service`` (``_liked_skill_shelf`` /
``_federated_liked_skills``, spotify_2607 Phase A/B) so any read surface that
needs to render a ``(BundleSkill.federated_source, BundleSkill.federated_slug)``
row resolves a human title the SAME way the Liked-library read path already
does, instead of a second surface re-deriving its own resolver and silently
drifting (the exact defect class this bug fix closes — see
``app/bundle_routes.py::_skills_for``'s INNER JOIN dropping federated rows
entirely, and the "dual-surface divergence" pattern in this repo's
loopskill-api-endpoint-development skill).
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.models import FederationHubSkill


def resolve_federated_hub_titles(db: Session, slugs: Iterable[str | None]) -> dict[str, FederationHubSkill]:
    """One batched ``federation_hub_skills`` lookup for the given slugs.

    Keep this batched — a per-row lookup here is an N+1 on any page that
    renders more than one federated row (mirrors the N+1 warning already
    documented on ``library_service._federated_liked_skills``).
    """
    slug_set = {s for s in slugs if s}
    if not slug_set:
        return {}
    rows = db.query(FederationHubSkill).filter(FederationHubSkill.slug.in_(slug_set)).all()
    return {row.slug: row for row in rows}


def federated_title_for(hub: FederationHubSkill | None, fallback_slug: str) -> str:
    """Resolve a human title for a federated row, failing soft to the slug.

    ``title`` is NOT NULL but defaults to ``""`` on ``FederationHubSkill`` —
    treat blank as unresolved. A row we never snapshotted (or one the hub
    later dropped) keeps the slug as its title: degraded, never a 500, and
    never a vanished entry (same fail-soft contract as the Liked-library
    federated shelf).
    """
    title = (hub.title or "").strip() if hub is not None else ""
    return title or fallback_slug
