"""Fleet MCP tools — create/subscribe/sync/list fleet operations.

Phase E — recipes_2005 sprint.

Tool signatures:
    loopskill_fleet_create(db, *, name, ctx) -> {fleet_id, fleet_key, name}
    loopskill_fleet_subscribe(db, *, fleet_id, cookbook_id, channel='stable', ctx) ->
        {fleet_id, cookbook_id, channel}
    loopskill_fleet_sync(db, *, fleet_id, dry_run=False, ctx) ->
        {fleet_id, cookbooks_synced: [{cookbook_id, changes:[...], applied:bool}]}
    loopskill_fleet_list(db, *, ctx) ->
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
from app.services.synthetic_runs import origin_verdict_for_key


# ── helpers ───────────────────────────────────────────────────────────────


def _generate_fleet_key() -> str:
    """Generate a new fleet API key in rec_fleet_<8hex>_<32hex> format."""
    prefix = secrets.token_hex(4)  # 8 hex chars
    body = secrets.token_hex(16)  # 32 hex chars
    return f"rec_fleet_{prefix}_{body}"


# ── tool implementations ──────────────────────────────────────────────────


def loopskill_fleet_create(
    db: Session,
    *,
    name: str,
    ctx: AuthContext,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Create a new named fleet for the authenticated user.

    Returns fleet_id, fleet_key (plaintext, shown ONCE), and name.
    The plaintext key is NOT stored — only its sha256 hash is persisted.

    mesh_0408/B' — optional org_id selector. A caller may belong to MULTIPLE
    orgs (OrgMembership is unique on (org_id, user_id), not on user_id alone)
    but until now the fleet always landed in whichever org happened to be the
    caller's OLDEST membership (_resolve_org_membership's tie-break), because
    ctx.org_id was the only signal available. This adds a way to pick a
    DIFFERENT one of the caller's own orgs — it is a SELECTION over a set the
    server has already independently verified via OrgMembership, not an
    ASSERTION of an arbitrary tenant string (that was T0-B, and is NOT this).

    Resolution:
      - org_id omitted/None → unchanged: use ctx.org_id (byte-identical to
        pre-existing behaviour, including for master, whose ctx.org_id is
        always None).
      - org_id provided → must parse as a UUID and must match an
        OrgMembership row for THIS caller's user_id. No such row → forbidden.
        Malformed UUID → invalid_org_id.
    """
    # Master callers can create fleets without a user_id (for admin use)
    if ctx.scope not in ("master", "user"):
        return {"error": "forbidden", "detail": "Must be authenticated to create a fleet"}

    owner_id = ctx.user_id

    resolved_org_id = ctx.org_id
    if org_id is not None:
        try:
            requested_org_uuid = UUID(org_id)
        except (ValueError, AttributeError, TypeError):
            return {"error": "invalid_org_id", "org_id": org_id}

        from app.models import OrgMembership

        membership = (
            db.query(OrgMembership)
            .filter(
                OrgMembership.org_id == requested_org_uuid,
                OrgMembership.user_id == owner_id,
            )
            .first()
        )
        if membership is None:
            return {"error": "forbidden", "detail": "Not a member of the requested org"}
        resolved_org_id = requested_org_uuid

    # Generate key and hash
    plaintext_key = _generate_fleet_key()
    key_hash = hashlib.sha256(plaintext_key.encode()).hexdigest()

    fleet = Fleet(
        id=uuid4(),
        owner_user_id=owner_id,
        name=name,
        fleet_api_key_hash=key_hash,
        # activate_0701/TEN: inherit org_id from the caller's tenant scope,
        # unless mesh_0408/B' narrowed it to a specific membership above.
        org_id=resolved_org_id,
        # mesh_0408 W4b: stamp an EXPLICIT origin verdict from the single
        # definition (APIKey.is_test) instead of leaving the row unclassified.
        # An unclassified fleet falls back to the known-beacon slug list, which
        # would count a customer's `p4-loop-proof` as our own traffic.
        is_synthetic=origin_verdict_for_key(db, ctx.api_key_id),
    )
    db.add(fleet)
    db.commit()

    return {
        "fleet_id": str(fleet.id),
        "fleet_key": plaintext_key,
        "name": fleet.name,
    }


def loopskill_fleet_subscribe(
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

    # portal_0610 R8: VALID_CHANNELS was defined but never enforced — channel
    # "turbo" was silently stored and treated as canary by channel_select. Reject
    # any channel outside the canonical set so a typo can't create an inert
    # subscription that looks subscribed but never syncs.
    from app.services.channel_select import VALID_CHANNELS

    if channel not in VALID_CHANNELS:
        return {
            "error": "invalid_channel",
            "channel": channel,
            "valid": sorted(VALID_CHANNELS),
        }

    fleet = db.query(Fleet).filter(Fleet.id == fleet_uuid).first()
    if fleet is None:
        return {"error": "not_found", "fleet_id": fleet_id}

    if not authz.can_use_fleet(ctx, fleet):
        return {"error": "forbidden", "fleet_id": fleet_id}

    try:
        cb_uuid = UUID(cookbook_id)
    except (ValueError, AttributeError):
        return {"error": "invalid_cookbook_id", "cookbook_id": cookbook_id}

    # activate_0701/TEN: org-scoped bundle access — a fleet in org A cannot
    # subscribe to org B's private bundle. Cross-org = forbidden.
    from app.models import Bundle

    bundle = db.query(Bundle).filter(Bundle.id == cb_uuid).first()
    if bundle is None:
        return {"error": "invalid_cookbook_id", "cookbook_id": cookbook_id}
    if not authz.can_access_bundle(ctx, bundle):
        return {"error": "forbidden", "cookbook_id": cookbook_id}

    # Idempotency: return existing row if present
    existing = (
        db.query(FleetSubscription)
        .filter(
            FleetSubscription.fleet_id == fleet_uuid,
            FleetSubscription.bundle_id == cb_uuid,
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
        bundle_id=cb_uuid,
        channel=channel,
    )
    db.add(sub)
    db.commit()

    return {
        "fleet_id": fleet_id,
        "cookbook_id": cookbook_id,
        "channel": channel,
    }


def loopskill_fleet_sync(
    db: Session,
    *,
    fleet_id: str,
    dry_run: bool = False,
    ctx: AuthContext,
) -> dict[str, Any]:
    """Synchronise all cookbooks subscribed to the fleet.

    Iterates fleet subscriptions and delegates each cookbook sync to the
    existing loopskill_sync service logic. Aggregates results across cookbooks.
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


def loopskill_fleet_list(
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
        # activate_0701/TEN: org members see all org fleets + their personal fleets.
        if ctx.org_id is not None:
            fleets = (
                db.query(Fleet)
                .filter((Fleet.owner_user_id == ctx.user_id) | (Fleet.org_id == ctx.org_id))
                .all()
            )
        else:
            fleets = db.query(Fleet).filter(Fleet.owner_user_id == ctx.user_id).all()
    elif ctx.scope == "fleet" and ctx.fleet_id is not None:
        # Fleet-scoped key: return only the one fleet
        fleet = db.query(Fleet).filter(Fleet.id == ctx.fleet_id).first()
        fleets = [fleet] if fleet else []
    else:
        return {"error": "forbidden", "detail": "Must be authenticated to list fleets"}

    result_fleets = []
    for fleet in fleets:
        subs = db.query(FleetSubscription).filter(FleetSubscription.fleet_id == fleet.id).all()
        result_fleets.append(
            {
                "fleet_id": str(fleet.id),
                "name": fleet.name,
                "subscriptions": [
                    {"cookbook_id": str(s.bundle_id), "channel": s.channel}  # compat-alias
                    for s in subs
                ],
            }
        )

    return {"fleets": result_fleets}
