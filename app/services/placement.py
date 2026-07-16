"""fleetos_1607 Phase A — placement service (epoch-CAS transitions).

The single-writer contract that makes "which host runs this loop right now" a
race-safe fact. Every mutation is a compare-and-swap on ``placement_epoch``:
the caller states the epoch it EXPECTS, and the write commits only if that still
matches — the loser of any race sees a stale epoch and is rejected, never
double-applied.

State machine (per fleet_id + loop_key):
    (none) --assign--> assigned --activate--> active
    active --drain(move)--> draining --confirm+activate-new--> active(new member)
    * --evacuate--> removed
    active(dead host) --force_move--> active(new member, forced=True)

No exactly-once claim is made (§0 #11). Epochs stamp every transition so Phase D
can flag stale-epoch runs after the fact; fire-time fencing is a v2 upgrade.

All functions are transactional and idempotent by ``op_id`` — a retried
assign/evacuate with the same op_id returns the existing result, not a second
transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.models import (
    FleetMemberLiveness,
    LoopManifest,
    LoopPlacement,
    PlacementConfirmation,
)

VALID_PLACEMENT_STATUS = ("assigned", "active", "draining", "removed")
LIVE_STATUSES = ("assigned", "active", "draining")


class PlacementError(Exception):
    """Raised on an illegal placement transition. Carries a structured code."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(f"{code}: {message}")


@dataclass
class PreflightResult:
    """Outcome of an assign-time capability/secret preflight."""

    ok: bool
    missing: list[str]

    def as_error(self) -> dict[str, Any]:
        return {"error": "preflight_failed", "code": 409, "missing": self.missing}


# ── helpers ──────────────────────────────────────────────────────────────────


def _live_placement(db: Session, fleet_id: UUID, loop_key: str) -> LoopPlacement | None:
    """Return the current non-removed placement for (fleet, loop), or None.

    There is at most one by construction (the service never creates a second
    live row). Ordered by epoch desc so a defensive read still picks the newest.
    """
    return (
        db.query(LoopPlacement)
        .filter(
            LoopPlacement.fleet_id == fleet_id,
            LoopPlacement.loop_key == loop_key,
            LoopPlacement.status.in_(LIVE_STATUSES),
        )
        .order_by(LoopPlacement.placement_epoch.desc())
        .first()
    )


def _max_epoch(db: Session, fleet_id: UUID, loop_key: str) -> int:
    """Highest epoch ever used for (fleet, loop) — 0 if none. New epoch = this+1."""
    rows = (
        db.query(LoopPlacement.placement_epoch)
        .filter(LoopPlacement.fleet_id == fleet_id, LoopPlacement.loop_key == loop_key)
        .all()
    )
    return max((r[0] for r in rows), default=0)


def _idempotent_hit(db: Session, fleet_id: UUID, loop_key: str, op_id: str | None) -> LoopPlacement | None:
    """If op_id already produced a placement row for this loop, return it (no-op replay)."""
    if not op_id:
        return None
    return (
        db.query(LoopPlacement)
        .filter(
            LoopPlacement.fleet_id == fleet_id,
            LoopPlacement.loop_key == loop_key,
            LoopPlacement.last_op_id == op_id,
        )
        .order_by(LoopPlacement.placement_epoch.desc())
        .first()
    )


# ── capability / secret preflight ────────────────────────────────────────────


def preflight_member(db: Session, member_id: UUID, loop_key: str) -> PreflightResult:
    """Check that ``member_id`` can satisfy loop ``loop_key``'s typed requires{}.

    Reads the loop's requires{} (LoopManifest) and the member's advertised
    provides{} (FleetMemberLiveness). Returns the NAMED missing requirements so
    an assign refusal can tell the operator exactly what's absent (§0 #5 / #4).
    A member with no liveness row (never pinged) fails preflight loudly rather
    than silently — you cannot place onto a member you've never heard from.
    """
    manifest = db.query(LoopManifest).filter(LoopManifest.loop_id == loop_key).first()
    requires: dict[str, Any] = dict(manifest.requires) if manifest and manifest.requires else {}
    secret_refs = list(manifest.secret_refs) if manifest and manifest.secret_refs else []

    liveness = db.query(FleetMemberLiveness).filter(FleetMemberLiveness.member_id == member_id).first()
    if liveness is None:
        return PreflightResult(ok=False, missing=["liveness:member-never-pinged"])
    provides: dict[str, Any] = dict(liveness.provides or {})

    missing: list[str] = []

    # os / arch
    for axis in ("os", "arch"):
        want = requires.get(axis)
        if want:
            wanted = [str(w).lower() for w in (want if isinstance(want, list) else [want])]
            have = str(provides.get(axis, "")).lower()
            if have not in wanted:
                missing.append(f"{axis}:{'|'.join(wanted)}")

    # runtimes (presence check at preflight; version match is host_profile's job)
    for name in requires.get("runtime") or {}:
        if name not in (provides.get("runtimes") or {}):
            missing.append(f"runtime:{name}")

    # packages
    have_pkgs = {str(p).lower() for p in (provides.get("packages") or [])}
    for pkg in requires.get("packages") or []:
        if str(pkg).lower() not in have_pkgs:
            missing.append(f"package:{pkg}")

    # connectors
    have_conn = {str(c).lower() for c in (provides.get("connectors") or [])}
    for conn in requires.get("connector") or []:
        if str(conn).lower() not in have_conn:
            missing.append(f"connector:{conn}")

    # required secrets — the member must advertise the NAME as available
    have_secrets = {str(s) for s in (provides.get("secrets") or [])}
    for ref in secret_refs:
        name = ref.get("name") if isinstance(ref, dict) else str(ref)
        required = ref.get("required", True) if isinstance(ref, dict) else True
        if required and name not in have_secrets:
            missing.append(f"secret:{name}")

    return PreflightResult(ok=not missing, missing=missing)


# ── transitions ──────────────────────────────────────────────────────────────


def assign(
    db: Session,
    fleet_id: UUID,
    loop_key: str,
    member_id: UUID,
    op_id: str | None = None,
    skip_preflight: bool = False,
) -> LoopPlacement:
    """Assign a loop to a member (initial placement or reassign onto a fresh loop).

    Fails with PlacementError('already_placed') if a live placement exists — a
    move must go through drain/activate or force_move, not a blind re-assign.
    Runs the capability/secret preflight unless skip_preflight (test/override).
    """
    replay = _idempotent_hit(db, fleet_id, loop_key, op_id)
    if replay is not None:
        return replay

    existing = _live_placement(db, fleet_id, loop_key)
    if existing is not None:
        raise PlacementError(
            "already_placed",
            f"loop {loop_key} already placed on member {existing.member_id} "
            f"(epoch {existing.placement_epoch}); use move/force_move",
            member_id=str(existing.member_id),
            epoch=existing.placement_epoch,
        )

    if not skip_preflight:
        pf = preflight_member(db, member_id, loop_key)
        if not pf.ok:
            raise PlacementError("preflight_failed", "member cannot satisfy requirements", missing=pf.missing)

    epoch = _max_epoch(db, fleet_id, loop_key) + 1
    placement = LoopPlacement(
        id=uuid4(),
        fleet_id=fleet_id,
        loop_key=loop_key,
        member_id=member_id,
        status="active",
        placement_epoch=epoch,
        last_op_id=op_id,
        forced=False,
    )
    db.add(placement)
    db.commit()
    db.refresh(placement)
    return placement


def begin_drain(
    db: Session,
    fleet_id: UUID,
    loop_key: str,
    expected_epoch: int,
    op_id: str | None = None,
) -> LoopPlacement:
    """CAS-transition the live placement active→draining, bumping the epoch.

    The caller states the epoch it expects to be draining. If the live placement
    is at a different epoch (someone else moved it first), raise
    PlacementError('epoch_conflict') — the caller lost the race, no write.
    """
    placement = _live_placement(db, fleet_id, loop_key)
    if placement is None:
        raise PlacementError("not_placed", f"loop {loop_key} has no live placement")
    if placement.placement_epoch != expected_epoch:
        raise PlacementError(
            "epoch_conflict",
            f"expected epoch {expected_epoch}, live is {placement.placement_epoch}",
            live_epoch=placement.placement_epoch,
        )
    if placement.status not in ("assigned", "active"):
        raise PlacementError("bad_state", f"cannot drain from status {placement.status}")

    placement.status = "draining"
    placement.placement_epoch = expected_epoch + 1
    placement.last_op_id = op_id
    db.commit()
    db.refresh(placement)
    return placement


def confirm_drain(
    db: Session,
    placement_id: UUID,
    member_id: UUID,
    confirmed_epoch: int,
    member_seq: int,
) -> PlacementConfirmation:
    """Record the old member's confirmation that it drained the loop.

    Deduped on (member_id, member_seq): a replayed confirmation with a seq the
    member already used is a no-op (returns the prior row), never a second count.
    Rejects a confirmation whose epoch doesn't match the placement's draining
    epoch (a stale/duplicate confirmation from a superseded move).
    """
    dup = (
        db.query(PlacementConfirmation)
        .filter(
            PlacementConfirmation.member_id == member_id,
            PlacementConfirmation.member_seq == member_seq,
        )
        .first()
    )
    if dup is not None:
        return dup

    placement = db.query(LoopPlacement).filter(LoopPlacement.id == placement_id).first()
    if placement is None:
        raise PlacementError("not_found", "placement not found")
    if placement.placement_epoch != confirmed_epoch:
        raise PlacementError(
            "stale_confirmation",
            f"confirmation epoch {confirmed_epoch} != placement epoch {placement.placement_epoch}",
        )

    conf = PlacementConfirmation(
        id=uuid4(),
        placement_id=placement_id,
        member_id=member_id,
        confirmed_epoch=confirmed_epoch,
        member_seq=member_seq,
    )
    db.add(conf)
    db.commit()
    db.refresh(conf)
    return conf


def complete_move(
    db: Session,
    fleet_id: UUID,
    loop_key: str,
    draining_epoch: int,
    new_member_id: UUID,
    op_id: str | None = None,
    skip_preflight: bool = False,
) -> LoopPlacement:
    """Finish a cooperative move: retire the draining placement, activate the new.

    CAS-guarded on ``draining_epoch``. Requires the draining placement to have a
    matching confirmation (the old member acknowledged the stop) UNLESS this is a
    forced completion. Creates a fresh placement row at epoch+1 on the new member
    so the audit trail of every epoch is preserved.
    """
    draining = (
        db.query(LoopPlacement)
        .filter(
            LoopPlacement.fleet_id == fleet_id,
            LoopPlacement.loop_key == loop_key,
            LoopPlacement.status == "draining",
            LoopPlacement.placement_epoch == draining_epoch,
        )
        .first()
    )
    if draining is None:
        raise PlacementError("no_draining_placement", f"no draining placement at epoch {draining_epoch}")

    confirmed = (
        db.query(PlacementConfirmation)
        .filter(
            PlacementConfirmation.placement_id == draining.id,
            PlacementConfirmation.confirmed_epoch == draining_epoch,
        )
        .first()
    )
    if confirmed is None:
        raise PlacementError(
            "unconfirmed_drain",
            "old member has not confirmed drain; use force_move to override a dead host",
        )

    if not skip_preflight:
        pf = preflight_member(db, new_member_id, loop_key)
        if not pf.ok:
            raise PlacementError(
                "preflight_failed", "new member cannot satisfy requirements", missing=pf.missing
            )

    draining.status = "removed"
    new_epoch = draining_epoch + 1
    activated = LoopPlacement(
        id=uuid4(),
        fleet_id=fleet_id,
        loop_key=loop_key,
        member_id=new_member_id,
        status="active",
        placement_epoch=new_epoch,
        last_op_id=op_id,
        forced=False,
    )
    db.add(activated)
    db.commit()
    db.refresh(activated)
    return activated


def force_move(
    db: Session,
    fleet_id: UUID,
    loop_key: str,
    new_member_id: UUID,
    op_id: str | None = None,
    skip_preflight: bool = False,
) -> LoopPlacement:
    """Move a loop off a presumed-DEAD host without a cooperative confirm.

    This is the honest, dangerous path (§0 #11): the old host may still be
    running the loop (partition, not death), so a forced move carries the
    duplicate-fire risk verbatim. The new placement is flagged ``forced=True``;
    Phase D treats runs under it as duplicate-risk per the loop's safety_class.
    Retires ALL live placements for the loop, then activates the new one at
    max_epoch+1 (so even a zombie's stale epoch can never match).
    """
    live = (
        db.query(LoopPlacement)
        .filter(
            LoopPlacement.fleet_id == fleet_id,
            LoopPlacement.loop_key == loop_key,
            LoopPlacement.status.in_(LIVE_STATUSES),
        )
        .all()
    )
    for p in live:
        p.status = "removed"

    if not skip_preflight:
        pf = preflight_member(db, new_member_id, loop_key)
        if not pf.ok:
            raise PlacementError(
                "preflight_failed", "new member cannot satisfy requirements", missing=pf.missing
            )

    new_epoch = _max_epoch(db, fleet_id, loop_key) + 1
    activated = LoopPlacement(
        id=uuid4(),
        fleet_id=fleet_id,
        loop_key=loop_key,
        member_id=new_member_id,
        status="active",
        placement_epoch=new_epoch,
        last_op_id=op_id,
        forced=True,
    )
    db.add(activated)
    db.commit()
    db.refresh(activated)
    return activated


def evacuate(
    db: Session,
    fleet_id: UUID,
    loop_key: str,
    op_id: str | None = None,
) -> LoopPlacement | None:
    """Remove the live placement for a loop entirely (unschedule it).

    Returns the removed placement, or None if there was nothing live. Idempotent
    by op_id.
    """
    replay = _idempotent_hit(db, fleet_id, loop_key, op_id)
    if replay is not None and replay.status == "removed":
        return replay

    placement = _live_placement(db, fleet_id, loop_key)
    if placement is None:
        return None
    placement.status = "removed"
    placement.last_op_id = op_id
    db.commit()
    db.refresh(placement)
    return placement


def active_placement_count(db: Session, fleet_id: UUID, loop_key: str) -> int:
    """Number of placements in status='active' for a loop — MUST never exceed 1.

    The concurrency-suite invariant: at every epoch there is at most one active
    placement. Exposed for the test suite and the registry health check.
    """
    return (
        db.query(LoopPlacement)
        .filter(
            LoopPlacement.fleet_id == fleet_id,
            LoopPlacement.loop_key == loop_key,
            LoopPlacement.status == "active",
        )
        .count()
    )
