"""Fleet MCP tools — create/subscribe/sync/list fleet operations.

Phase E — recipes_2005 sprint.

Tool signatures:
    recipes_fleet_create(db, *, name, ctx) -> {fleet_id, fleet_key, name}
    recipes_fleet_subscribe(db, *, fleet_id, cookbook_id, channel='stable', ctx) ->
        {fleet_id, cookbook_id, channel}
    recipes_fleet_sync(db, *, fleet_id, dry_run=False, ctx) ->
        {fleet_id, cookbooks_synced: [{cookbook_id, changes:[...], applied:bool}]}
    recipes_fleet_list(db, *, ctx) ->
        {fleets: [{fleet_id, name, subscriptions:[{cookbook_id, channel}]}]}

Fleet key format: rec_fleet_<8hex>_<32hex>
Stored as sha256 hash in Fleet.fleet_api_key_hash.
Plaintext shown ONCE on create.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.models import Fleet, FleetSubscription


# ── helpers ───────────────────────────────────────────────────────────────


def _generate_fleet_key() -> str:
    """Generate a new fleet API key in rec_fleet_<8hex>_<32hex> format."""
    prefix = secrets.token_hex(4)   # 8 hex chars
    body = secrets.token_hex(16)    # 32 hex chars
    return f"rec_fleet_{prefix}_{body}"


# ── tool implementations ──────────────────────────────────────────────────


def recipes_fleet_create(
    db: Session,
    *,
    name: str,
    ctx: AuthContext,
) -> dict[str, Any]:
    """Create a new named fleet for the authenticated user.

    Returns fleet_id, fleet_key (plaintext, shown ONCE), and name.
    The plaintext key is NOT stored — only its sha256 hash is persisted.
    """
    # Master callers can create fleets without a user_id (for admin use)
    if ctx.scope not in ("master", "user"):
        return {"error": "forbidden", "detail": "Must be authenticated to create a fleet"}

    owner_id = ctx.user_id

    # Generate key and hash
    plaintext_key = _generate_fleet_key()
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()

    fleet = Fleet(
        id=uuid4(),
        owner_user_id=owner_id,
        name=name,
        fleet_api_key_hash=key_hash,
    )
    db.add(fleet)
    db.commit()

    return {
        "fleet_id": str(fleet.id),
        "fleet_key": plaintext_key,
        "name": fleet.name,
    }


def recipes_fleet_subscribe(
    db: Session,
    *,
    fleet_id: str,
    cookbook_id: str,
    channel: str = "stable",
    ctx: AuthContext,
) -> dict[str, Any]:
    """Subscribe a cookbook to a fleet on the given channel.

    Idempotent — calling twice with the same args is safe. If the subscription
    already exists it is returned unchanged (channel update is NOT performed on
    re-subscribe to preserve immutability semantics; create a new subscription
    with a different channel if desired).
    """
    try:
        fleet_uuid = UUID(fleet_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_fleet_id", "fleet_id": fleet_id}

    fleet = db.query(Fleet).filter(Fleet.id == fleet_uuid).first()
    if fleet is None:
        return {"error": "not_found", "fleet_id": fleet_id}

    if not authz.can_use_fleet(ctx, fleet):
        return {"error": "forbidden", "fleet_id": fleet_id}

    try:
        cb_uuid = UUID(cookbook_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_cookbook_id", "cookbook_id": cookbook_id}

    # Idempotency: return existing row if present
    existing = (
        db.query(FleetSubscription)
        .filter(
            FleetSubscription.fleet_id == fleet_uuid,
            FleetSubscription.cookbook_id == cb_uuid,
        )
        .first()
    )
    if existing is not None:
        return {
            "fleet_id": fleet_id,
            "cookbook_id": cookbook_id,
            "channel": existing.channel,
        }

    sub = FleetSubscription(
        fleet_id=fleet_uuid,
        cookbook_id=cb_uuid,
        channel=channel,
    )
    db.add(sub)
    db.commit()

    return {
        "fleet_id": fleet_id,
        "cookbook_id": cookbook_id,
        "channel": channel,
    }


def recipes_fleet_sync(
    db: Session,
    *,
    fleet_id: str,
    dry_run: bool = False,
    ctx: AuthContext,
) -> dict[str, Any]:
    """Synchronise all cookbooks subscribed to the fleet.

    Iterates fleet subscriptions and delegates each cookbook sync to the
    existing recipes_sync service logic. Aggregates results across cookbooks.
    """
    from app.services.fleet_sync import sync_fleet

    try:
        fleet_uuid = UUID(fleet_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_fleet_id", "fleet_id": fleet_id}

    fleet = db.query(Fleet).filter(Fleet.id == fleet_uuid).first()
    if fleet is None:
        return {"error": "not_found", "fleet_id": fleet_id}

    if not authz.can_use_fleet(ctx, fleet):
        return {"error": "forbidden", "fleet_id": fleet_id}

    cookbooks_synced = sync_fleet(db, fleet_uuid, dry_run=dry_run, ctx=ctx)

    return {
        "fleet_id": fleet_id,
        "cookbooks_synced": cookbooks_synced,
    }


def recipes_fleet_list(
    db: Session,
    *,
    ctx: AuthContext,
) -> dict[str, Any]:
    """List fleets owned by the authenticated user, with their subscriptions.

    Master callers see all fleets. User callers see only their own.
    """
    if ctx.scope == "master":
        fleets = db.query(Fleet).all()
    elif ctx.scope == "user" and ctx.user_id is not None:
        fleets = (
            db.query(Fleet).filter(Fleet.owner_user_id == ctx.user_id).all()
        )
    elif ctx.scope == "fleet" and ctx.fleet_id is not None:
        # Fleet-scoped key: return only the one fleet
        fleet = db.query(Fleet).filter(Fleet.id == ctx.fleet_id).first()
        fleets = [fleet] if fleet else []
    else:
        return {"error": "forbidden", "detail": "Must be authenticated to list fleets"}

    result_fleets = []
    for fleet in fleets:
        subs = (
            db.query(FleetSubscription)
            .filter(FleetSubscription.fleet_id == fleet.id)
            .all()
        )
        result_fleets.append(
            {
                "fleet_id": str(fleet.id),
                "name": fleet.name,
                "subscriptions": [
                    {"cookbook_id": str(s.cookbook_id), "channel": s.channel}
                    for s in subs
                ],
            }
        )

    return {"fleets": result_fleets}
