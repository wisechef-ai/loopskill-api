"""mesh_0408 W4 — separate SELF-ORIGINATED loop runs from external ones.

The defect this closes: LoopSkill's own ``*/3min`` beacon (``p4-loop-proof``)
emits ~480 runs/day, so a raw ``loop_runs`` count reads 1760 while exactly ONE
of those runs came from a loop somebody else installed. A product that counts
its own heartbeat as adoption lies to the people deciding what to build next.
So: every surface that reports a run count must report BOTH numbers, and must
never present the combined figure on its own.

**Where the marker lives.** ``APIKey.is_test`` (spotify_0608/B) already set the
precedent for install counts — flag at the identity level, filter at count
time. That is the SINGLE definition of "this traffic is ours"; this module does
not invent a competing one, it widens the same idea to the two fleet-side
identities so a whole fleet or a single agent can be classified without
minting keys:

  * ``FleetMember.is_synthetic`` — this one agent is ours, in an otherwise real fleet
  * ``APIKey.is_test``           — the member's own key (the existing precedent)
  * ``Fleet.is_synthetic``       — the whole fleet is ours (a CI / harness fleet)

...consulted in exactly that order, MOST SPECIFIC FIRST. The two fleet-side
columns are THREE-VALUED: ``NULL`` means nobody has classified this identity.
``APIKey.is_test`` is not — it is a long-standing ``NOT NULL DEFAULT false``
column, so only its ``True`` direction carries information here; reading its
``False`` as "explicitly a customer's" would make every key ever minted an
explicit verdict and disable the backstop below.

**Lifecycle.** A marker no code path can set is not a marker — it is a
hard-coded slug list wearing a column. So:

  * every creation path stamps an EXPLICIT verdict from the caller's
    ``APIKey.is_test`` (``app/mcp/tools/fleet.py``, ``app/fleet_member_routes.py``),
    which is why new rows are never ``NULL``;
  * :func:`set_fleet_origin` / :func:`set_member_origin` set it afterwards, and
    REPAIR the runs already ingested under the old verdict (see below).

A run's classification is DENORMALIZED onto ``LoopRun.is_synthetic`` at ingest.
Runs are immutable facts; deciding at count time would silently re-classify
history every time a flag is toggled, and would need a three-table join on the
hottest read path in the registry. The cost of that choice is that a late flag
does not repair itself — which is exactly why the setters do it explicitly,
across the raw rows AND the daily rollup that outlives them.

``SELF_ORIGINATED_LOOP_SLUGS`` is a BACKSTOP, not the definition. It is
consulted only for an identity nobody has classified — so the known beacon
reads correctly on a pre-W4 fleet, and the backfill migration has a seed —
and it always loses to an explicit verdict. When it was the primary signal it
was wrong in both directions: a customer who declared ``p4-loop-proof``
(``loop_slug`` gets only length/non-empty validation at ingest) was counted as
ours, and a second internal beacon under any other name was counted as
adoption. The second is the one that matters most: the number that must never
be inflated is the external one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import APIKey, Fleet, FleetMember, LoopRun, LoopRunDailyRollup

__all__ = [
    "SELF_ORIGINATED_LOOP_SLUGS",
    "RunCounts",
    "classify_run_synthetic",
    "member_is_synthetic",
    "origin_verdict_for_key",
    "origin_verdict_for_member",
    "set_fleet_origin",
    "set_member_origin",
]

# Loop slugs LoopSkill runs against its own fleets as proof-of-life. A
# backstop for un-flagged fleets and the seed for the backfill migration —
# never the whole definition of "synthetic". See module docstring.
SELF_ORIGINATED_LOOP_SLUGS = frozenset({"p4-loop-proof"})


@dataclass(frozen=True)
class RunCounts:
    """Run totals split by origin. ``total`` is never reported without the split."""

    total: int
    synthetic: int

    @property
    def external(self) -> int:
        """Runs from somebody who is not us — the only adoption-bearing number."""
        return self.total - self.synthetic

    def to_dict(self) -> dict[str, int]:
        return {"total": self.total, "synthetic": self.synthetic, "external": self.external}


def origin_verdict_for_member(db: Session, member: FleetMember | None) -> bool | None:
    """The IDENTITY's verdict for this member: ours / theirs / nobody has said.

    ``None`` is a real answer, not a failure — it is what lets the slug
    backstop apply to un-classified identities and only to those. Resolution
    is most-specific-first:

      1. ``FleetMember.is_synthetic`` — the per-agent claim. Lock #13 makes the
         per-agent key the member identity, so nothing is more specific; an
         explicit ``False`` here survives an internal fleet-level ``True``.
      2. ``APIKey.is_test`` — the same specificity, and the pre-existing
         definition (spotify_0608/B). Only its ``True`` direction is a signal:
         the column is ``NOT NULL DEFAULT false``, so treating ``False`` as an
         explicit verdict would classify every key ever minted.
      3. ``Fleet.is_synthetic`` — the whole fleet, which is how one flag covers
         every member under it without a per-member sweep.
    """
    if member is None:
        return None

    if member.is_synthetic is not None:
        return bool(member.is_synthetic)

    if member.api_key_id is not None:
        key = db.query(APIKey).filter(APIKey.id == member.api_key_id).first()
        if key is not None and bool(key.is_test):
            return True

    fleet = db.query(Fleet).filter(Fleet.id == member.fleet_id).first()
    if fleet is not None and fleet.is_synthetic is not None:
        return bool(fleet.is_synthetic)

    return None


def origin_verdict_for_key(db: Session, api_key_id: UUID | None) -> bool | None:
    """The creation-time verdict for a caller holding ``api_key_id``.

    Used by the fleet/member creation paths to stamp an EXPLICIT marker from
    the single definition (``APIKey.is_test``) rather than leaving the row
    unclassified and hoping the slug backstop covers it. ``None`` when there is
    no key to ask (master scope, or an anonymous internal caller) — honest, and
    it leaves the backstop in play for exactly that row.
    """
    if api_key_id is None:
        return None
    key = db.query(APIKey).filter(APIKey.id == api_key_id).first()
    if key is None:
        return None
    return bool(key.is_test)


def member_is_synthetic(db: Session, member: FleetMember | None) -> bool:
    """True when this member's traffic is LoopSkill's own, not a customer's.

    The boolean projection of :func:`origin_verdict_for_member` — an
    unclassified identity is not a claim that the traffic is ours, so ``None``
    reads as False here. Callers that need to distinguish "nobody has said"
    from "explicitly a customer's" must use the verdict function directly.
    """
    return origin_verdict_for_member(db, member) is True


def classify_run_synthetic(
    db: Session,
    *,
    loop_slug: str | None,
    member: FleetMember | None = None,
    member_id: UUID | None = None,
) -> bool:
    """Decide whether ONE run about to be recorded is self-originated.

    Called at ingest so the verdict is frozen onto the ``LoopRun`` row. Pass
    the already-loaded ``member`` when the caller has one; ``member_id`` is
    the fallback for callers that only carry the id.

    Identity first, ALWAYS. The slug backstop is reached only when no identity
    has been classified — otherwise a customer who declares ``p4-loop-proof``
    is counted as our own beacon, and an internal beacon under a new name is
    counted as adoption.
    """
    if member is None and member_id is not None:
        member = db.query(FleetMember).filter(FleetMember.id == member_id).first()

    verdict = origin_verdict_for_member(db, member)
    if verdict is not None:
        return verdict

    return bool(loop_slug) and loop_slug in SELF_ORIGINATED_LOOP_SLUGS


def _repair_ingested_runs(db: Session, *, synthetic: bool, member_ids: list[UUID]) -> int:
    """Re-stamp already-ingested runs after an identity's verdict changed.

    The per-run verdict is frozen at ingest (a run is an immutable fact and no
    read path should carry a three-table join), so flipping a marker leaves
    history stating the old answer forever unless it is rewritten here.

    Both the raw rows AND the daily rollup are updated: raw rows are pruned at
    30d, so the rollup is the only place the split survives past a month — a
    repair that skipped it would silently un-repair itself.
    """
    if not member_ids:
        return 0

    n = (
        db.query(LoopRun)
        .filter(LoopRun.member_id.in_(member_ids))
        .update({LoopRun.is_synthetic: synthetic}, synchronize_session=False)
    )
    # The rollup is keyed per (fleet, member, loop, day), so an affected row is
    # wholly synthetic or wholly not — no partial recount is possible.
    db.query(LoopRunDailyRollup).filter(LoopRunDailyRollup.member_id.in_(member_ids)).update(
        {LoopRunDailyRollup.synthetic_runs: LoopRunDailyRollup.runs if synthetic else 0},
        synchronize_session=False,
    )
    db.commit()
    db.expire_all()
    return n


def set_member_origin(db: Session, member: FleetMember, *, synthetic: bool) -> FleetMember:
    """Classify ONE agent after the fact, repairing the runs it already emitted.

    The after-creation half of the marker lifecycle. Use it to flag an internal
    beacon host inside an otherwise-real fleet, or to correct a
    misclassification in either direction — under-stating adoption is as much a
    defect as over-stating it, so this is deliberately not one-way.
    """
    member.is_synthetic = synthetic
    db.add(member)
    _repair_ingested_runs(db, synthetic=synthetic, member_ids=[member.id])
    return member


def set_fleet_origin(db: Session, fleet: Fleet, *, synthetic: bool) -> Fleet:
    """Classify a WHOLE fleet after the fact, repairing its members' runs.

    Members carrying their own explicit ``is_synthetic`` are left alone: the
    per-agent verdict is more specific than the fleet's, and silently
    overwriting it here would make the specificity order a lie.
    """
    fleet.is_synthetic = synthetic
    db.add(fleet)
    member_ids = [
        m.id
        for m in db.query(FleetMember).filter(FleetMember.fleet_id == fleet.id).all()
        if m.is_synthetic is None
    ]
    _repair_ingested_runs(db, synthetic=synthetic, member_ids=member_ids)
    return fleet


def split_counts(rows: list[Any], *, is_synthetic=lambda r: bool(r.is_synthetic)) -> RunCounts:
    """Fold an iterable of run-ish rows into a :class:`RunCounts`."""
    total = len(rows)
    synthetic = sum(1 for r in rows if is_synthetic(r))
    return RunCounts(total=total, synthetic=synthetic)
