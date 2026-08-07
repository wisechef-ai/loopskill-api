"""mesh_0408 W4 — separate SELF-ORIGINATED loop runs from external ones.

The defect this closes: LoopSkill's own ``*/3min`` beacon (``p4-loop-proof``)
emits ~480 runs/day, so a raw ``loop_runs`` count reads 1760 while exactly ONE
of those runs came from a loop somebody else installed. A product that counts
its own heartbeat as adoption lies to the people deciding what to build next.
So: every surface that reports a run count must report BOTH numbers, and must
never present the combined figure on its own.

**Where the marker lives.** ``APIKey.is_test`` (spotify_0608/B) already set the
precedent for install counts — flag at the identity level, filter at count
time. This module follows it and widens it to the two fleet-side identities:

  * ``Fleet.is_synthetic``       — the whole fleet is ours (a CI / harness fleet)
  * ``FleetMember.is_synthetic`` — this one agent is ours, in an otherwise real fleet
  * ``APIKey.is_test``           — inherited via the member's key (the existing precedent)

A run's classification is then DENORMALIZED onto ``LoopRun.is_synthetic`` at
ingest. Runs are immutable facts; deciding at count time would silently
re-classify history every time a flag is toggled, and would need a three-table
join on the hottest read path in the registry.

``SELF_ORIGINATED_LOOP_SLUGS`` is a documented backstop, NOT the definition —
it exists so the known beacon is classified correctly even on a fleet nobody
has flagged yet, and so the backfill migration has a seed. The failure mode it
introduces is deliberate: an external user who names their loop
``p4-loop-proof`` gets counted as synthetic, which UNDER-states adoption. Every
approximation in this module is biased that way on purpose — the number that
must never be inflated is the external one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import APIKey, Fleet, FleetMember

__all__ = [
    "SELF_ORIGINATED_LOOP_SLUGS",
    "RunCounts",
    "classify_run_synthetic",
    "member_is_synthetic",
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


def member_is_synthetic(db: Session, member: FleetMember | None) -> bool:
    """True when this member's traffic is LoopSkill's own, not a customer's.

    Checks the member flag, then the owning fleet's flag, then the member's
    API key ``is_test`` (the spotify_0608/B precedent). Any one of the three
    is sufficient — the flags are an OR, so marking a fleet synthetic marks
    everything under it without a per-member sweep.
    """
    if member is None:
        return False
    if bool(getattr(member, "is_synthetic", False)):
        return True

    fleet = db.query(Fleet).filter(Fleet.id == member.fleet_id).first()
    if fleet is not None and bool(getattr(fleet, "is_synthetic", False)):
        return True

    if member.api_key_id is not None:
        key = db.query(APIKey).filter(APIKey.id == member.api_key_id).first()
        if key is not None and bool(key.is_test):
            return True

    return False


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
    """
    if loop_slug and loop_slug in SELF_ORIGINATED_LOOP_SLUGS:
        return True

    if member is None and member_id is not None:
        member = db.query(FleetMember).filter(FleetMember.id == member_id).first()

    return member_is_synthetic(db, member)


def split_counts(rows: list[Any], *, is_synthetic=lambda r: bool(r.is_synthetic)) -> RunCounts:
    """Fold an iterable of run-ish rows into a :class:`RunCounts`."""
    total = len(rows)
    synthetic = sum(1 for r in rows if is_synthetic(r))
    return RunCounts(total=total, synthetic=synthetic)
