"""The ONE implementation of "how many bundles count against a tier's cap".

autopilot_0308 M1. Before this module, ``bundle_routes.create_cookbook`` and the
MCP verb ``loopskill_compose_bundle_from_links`` each ran their own
``db.query(Bundle).filter(Bundle.bundle_owner == uid).count()``. Two copies of a
rule is how a UI ends up showing a limit the API does not enforce — so the count
lives here, once, and every enforcer and every display surface reads it.

The rule (hub decision D-011):

    The cap meters bundles that are NOT public. PUBLIC bundles are unlimited on
    every tier, including Free.

Public curation is a growth lever — free community curation that scales the
offering by cross-matching across federations — so the platform must never
charge a curator a slot for publishing.

``visibility`` has three values, not two: ``private`` | ``team`` | ``public``.
M1 decided that ``team`` IS metered, i.e. the predicate is ``!= 'public'`` rather
than ``== 'private'``. Two reasons: the cap exists to meter private-to-you
capacity, and ``team`` is not free community curation; and metering only
``private`` would be a one-click cap bypass (flip everything to ``team`` and own
unlimited quasi-private bundles).

Owner-less rows (``bundle_owner IS NULL`` — the ``is_base`` "WiseChef Recipes
Catalog" bundle is owner-less BY DESIGN) belong to nobody and are counted
against nobody.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Bundle
from app.services.conversion_gates import gate_cookbook_create
from app.tier_labels import bundle_limit

#: The one visibility value that is never metered (D-011).
UNMETERED_VISIBILITY = "public"


def count_metered_bundles(db: Session, user_id: UUID | str | None) -> int:
    """Return how many of ``user_id``'s bundles count against their tier cap.

    Everything the user owns whose ``visibility`` is not ``'public'`` — so
    ``private`` and ``team`` count, ``public`` never does.

    ``user_id`` of ``None`` returns 0 rather than degrading into
    ``bundle_owner IS NULL``, which would count the owner-less base catalog
    bundle against an anonymous caller.
    """
    if user_id is None:
        return 0
    return (
        db.query(Bundle)
        .filter(
            Bundle.bundle_owner == user_id,  # compat-alias
            # `visibility` is NOT NULL in the model, but SQL three-valued logic
            # would silently drop any legacy NULL row from a `!= 'public'`
            # comparison — and a dropped row is a free slot. Be explicit.
            or_(Bundle.visibility.is_(None), Bundle.visibility != UNMETERED_VISIBILITY),
        )
        .count()
    )


def quota_status(db: Session, user_id: UUID | str | None, tier: str | None) -> dict[str, Any]:
    """Return the caller's private-bundle quota: ``{used, limit, blocked}``.

    ``limit`` is the tier cap from the ``config/tiers.yaml`` SSOT (``None`` =
    unlimited; reserved, no current tier is unlimited). ``blocked`` is the
    create decision, delegated to ``conversion_gates.gate_cookbook_create`` so
    the conversion ladder and the enforcer can never disagree about where the
    ceiling is.

    Both enforcers (REST + MCP) and both display surfaces (``/api/billing/me``,
    ``/api/auth/me``) call this, so the number a user is shown is by
    construction the number they are held to.
    """
    limit = bundle_limit(tier)
    used = count_metered_bundles(db, user_id)
    outcome = gate_cookbook_create(tier, current_count=used, limit=limit)
    return {"used": used, "limit": limit, "blocked": not outcome.allowed}
