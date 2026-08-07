"""The counter a future meter attaches to. NOT a meter, and not billing.

LoopSkill has a **CAP** (``TIER_KEY_CAPS`` / ``bundle_limit`` → HTTP 402) but no
**METER**. A flat-fee customer pays the same whether they run 1 unit or the cap,
so usage growth produces no revenue growth — the structural reason the product
cannot "earn while we sleep". ``FleetMember``'s own docstring calls the agent API
key *"the billable + identity primitive"*, yet ``fleet_member_routes.py`` imports
zero billing modules.

This module closes the *instrumentation* half of that gap and nothing more. It
adds **no price, no Stripe usage record, and no metered SKU** — lock #24 forbids
any phase from adding or proposing a price, and that holds here. What it adds is
a queryable, per-org, per-period count of the billable-CANDIDATE unit, so that
deciding to meter later is a config change against a number that already exists
and has already been reconciled, instead of an integration project starting from
zero history.

**Synthetic must stay separable.** ``loop_runs`` is dominated by LoopSkill's own
``*/3min`` self-beacon: at the time of writing 1759 of 1760 rows are one
internal loop. A usage count that cannot exclude that is not a usage count. The
marker used here is the EXISTING one:

    ``api_keys.is_test`` — set on keys whose traffic is test/CI/internal harness

It is already the marker for the same distinction on the public-ranking surfaces
(``app/_skill_helpers.py``, ``app/core_routes.py``), so this module reuses it
rather than inventing a second, divergent notion of "synthetic". A fleet member
is synthetic when its API key is flagged; a loop run is synthetic when the member
that emitted it is. Runs whose ``member_id`` resolves to no member are neither —
they are reported separately as *unattributed*, because "we cannot attribute
this" must not quietly round down to "not billable".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import case, func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: The synthetic marker, named so callers and reviewers can grep for the
#: decision rather than re-deriving it. Reused from the install-count integrity
#: work (spotify_0608 Ph B §4.2) — do NOT introduce a second one.
SYNTHETIC_MARKER = "api_keys.is_test"


@dataclass
class OrgBillableUnits:
    """Billable-candidate counts for one tenant over one period.

    ``org_id`` is None for personal-scope fleets (``Fleet.org_id IS NULL``),
    which is a real bucket, not an error.

    Every field is a COUNT of a candidate unit. None of them is a charge.
    """

    org_id: UUID | None
    org_name: str | None
    # Active enrolled agents — the per-seat candidate.
    active_fleet_members: int = 0
    active_fleet_members_synthetic: int = 0
    # Loop runs in the period — the per-run candidate.
    loop_runs: int = 0
    loop_runs_synthetic: int = 0
    loop_runs_unattributed: int = 0

    @property
    def billable_candidate_members(self) -> int:
        """Active members excluding synthetic ones. The seat-metering candidate."""
        return self.active_fleet_members

    @property
    def billable_candidate_runs(self) -> int:
        """Loop runs attributable to a non-synthetic member. The usage candidate."""
        return self.loop_runs


@dataclass
class BillableUnitsReport:
    """The whole per-org breakdown plus the period it was measured over."""

    period_start: datetime
    period_end: datetime
    orgs: list[OrgBillableUnits] = field(default_factory=list)


def current_period(now: datetime | None = None) -> tuple[datetime, datetime]:
    """The current billing-period proxy: start of the UTC calendar month → now.

    A calendar month is deliberate: it is the period a monthly subscription is
    already denominated in, so a future meter reconciles against the same window
    Stripe invoices on. It is a PROXY — Stripe anchors each subscription's period
    to its own creation date, and aligning the two is part of the metering
    decision, not of counting.
    """
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), now


def billable_units(
    db: Session,
    *,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    org_id: UUID | None = None,
) -> BillableUnitsReport:
    """Count billable-candidate units per org for a period.

    Args:
        db: session.
        period_start / period_end: half-open window ``[start, end)`` applied to
            ``LoopRun.created_at``. Defaults to :func:`current_period`. Member
            counts are a point-in-time snapshot (who is enrolled NOW) and are
            deliberately not windowed — a seat is a state, not an event.
        org_id: narrow to one tenant. The row is returned even when every count
            is zero, so a caller gets a definite answer rather than an empty
            list it has to interpret.

    Returns a :class:`BillableUnitsReport`. Never raises on missing rows.
    """
    from app.models import APIKey, Fleet, FleetMember, LoopRun, Org

    if period_start is None or period_end is None:
        default_start, default_end = current_period()
        period_start = period_start or default_start
        period_end = period_end or default_end

    rows: dict[UUID | None, OrgBillableUnits] = {}

    def _row(key: UUID | None) -> OrgBillableUnits:
        if key not in rows:
            rows[key] = OrgBillableUnits(org_id=key, org_name=None)
        return rows[key]

    # ── Active enrolled agents, split by the synthetic marker ──────────────
    # coalesce so a member whose key row is missing counts as organic, matching
    # the existing convention on the install-count surfaces.
    is_synthetic = func.coalesce(APIKey.is_test, False)
    member_q = (
        db.query(Fleet.org_id, is_synthetic.label("synthetic"), func.count(FleetMember.id))
        .join(Fleet, Fleet.id == FleetMember.fleet_id)
        .outerjoin(APIKey, APIKey.id == FleetMember.api_key_id)
        .filter(FleetMember.is_active.is_(True))
        .group_by(Fleet.org_id, is_synthetic)
    )
    if org_id is not None:
        member_q = member_q.filter(Fleet.org_id == org_id)
    for scope, synthetic, count in member_q.all():
        row = _row(scope)
        if synthetic:
            row.active_fleet_members_synthetic += int(count or 0)
        else:
            row.active_fleet_members += int(count or 0)

    # ── Loop runs in the period, split three ways ─────────────────────────
    # LEFT JOIN on fleets because LoopRun.fleet_id carries no FK; a run whose
    # fleet no longer exists lands in the None (personal-or-unscoped) bucket
    # rather than vanishing from the count.
    bucket = case(
        (FleetMember.id.is_(None), "unattributed"),
        (func.coalesce(APIKey.is_test, False).is_(True), "synthetic"),
        else_="billable",
    )
    run_q = (
        db.query(Fleet.org_id, bucket.label("bucket"), func.count(LoopRun.id))
        .outerjoin(Fleet, Fleet.id == LoopRun.fleet_id)
        .outerjoin(FleetMember, FleetMember.id == LoopRun.member_id)
        .outerjoin(APIKey, APIKey.id == FleetMember.api_key_id)
        .filter(LoopRun.created_at >= period_start, LoopRun.created_at < period_end)
        .group_by(Fleet.org_id, bucket)
    )
    if org_id is not None:
        run_q = run_q.filter(Fleet.org_id == org_id)
    for scope, bucket_name, count in run_q.all():
        row = _row(scope)
        n = int(count or 0)
        if bucket_name == "synthetic":
            row.loop_runs_synthetic += n
        elif bucket_name == "unattributed":
            row.loop_runs_unattributed += n
        else:
            row.loop_runs += n

    if org_id is not None:
        _row(org_id)  # always answer for an explicitly requested tenant

    # ── Names, one round trip ──────────────────────────────────────────────
    named = [k for k in rows if k is not None]
    if named:
        for oid, name in db.query(Org.id, Org.name).filter(Org.id.in_(named)).all():
            rows[oid].org_name = name

    ordered = sorted(
        rows.values(),
        # Largest tenant first; the personal (None) bucket sorts last on ties.
        key=lambda r: (-(r.active_fleet_members + r.loop_runs), str(r.org_id or "~")),
    )
    return BillableUnitsReport(period_start=period_start, period_end=period_end, orgs=ordered)
