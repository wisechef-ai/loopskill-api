"""Fleet sync service — aggregate sync across all subscribed cookbooks.

Called by recipes_fleet_sync MCP tool. Iterates the fleet's FleetSubscription
rows and delegates each to the existing recipes_sync internals, then aggregates
the per-cookbook results.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.auth_ctx import AuthContext
from app.models import FleetSubscription


def sync_fleet(
    db: Session,
    fleet_id: UUID,
    *,
    dry_run: bool = False,
    ctx: AuthContext,
) -> list[dict[str, Any]]:
    """Iterate subscriptions for *fleet_id* and run sync on each cookbook.

    Returns a list of per-cookbook sync results, each shaped::

        {
            "cookbook_id": str,
            "changes": [...],
            "applied": bool,
        }
    """
    from app.mcp.tools.recipes_sync import recipes_sync

    # Pull all subscription rows for this fleet
    subs = db.query(FleetSubscription).filter(FleetSubscription.fleet_id == fleet_id).all()

    results: list[dict[str, Any]] = []
    for sub in subs:
        cb_id = str(sub.cookbook_id)
        sync_result = recipes_sync(
            db,
            cookbook_id=cb_id,
            dry_run=dry_run,
            ctx=ctx,
        )
        # Normalise to the fleet_sync shape
        results.append(
            {
                "cookbook_id": cb_id,
                "changes": sync_result.get("changes", []),
                "applied": sync_result.get("applied", not dry_run),
                "channel": sub.channel,
            }
        )

    return results
