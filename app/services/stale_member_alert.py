"""fleetos_1607 Phase A — stale-member alert (the failover replacement).

§0 #16a deleted Phase F failover. Its operator outcome — "a host went dark, do
something" — is delivered instead by a detector that flags members whose
operational ping is older than a multiple of their reconcile interval, plus a
MANUAL evacuate the operator runs. Alert only; no automatic reassignment. The
trust ledger watches this; auto-failover earns its way in later once the ledger
has data to justify it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import FleetMember, FleetMemberLiveness

# A member is "stale" when its last ping is older than this multiple of its
# declared reconcile interval. 3× tolerates one or two missed cycles before
# crying wolf (a single slow tick is not a dead host).
STALE_MULTIPLE = 3


@dataclass
class StaleMember:
    member_id: str
    fleet_id: str
    host: str
    last_ping_at: datetime | None
    seconds_since_ping: float | None
    reconcile_interval_seconds: int


def find_stale_members(db: Session, now: datetime | None = None) -> list[StaleMember]:
    """Return active members whose liveness ping is older than STALE_MULTIPLE×interval.

    A member that has never pinged (no liveness row) but is active is also
    surfaced — an enrolled-but-silent member is exactly the dark-host case the
    alert exists for.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    stale: list[StaleMember] = []
    active_members = db.query(FleetMember).filter(FleetMember.is_active == True).all()  # noqa: E712
    liveness_by_member = {lv.member_id: lv for lv in db.query(FleetMemberLiveness).all()}

    for m in active_members:
        lv = liveness_by_member.get(m.id)
        if lv is None:
            # Enrolled but never pinged — dark from birth.
            stale.append(
                StaleMember(
                    member_id=str(m.id),
                    fleet_id=str(m.fleet_id),
                    host=m.host,
                    last_ping_at=None,
                    seconds_since_ping=None,
                    reconcile_interval_seconds=300,
                )
            )
            continue

        last = lv.last_ping_at
        # Normalize to aware UTC for the delta.
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        interval = lv.reconcile_interval_seconds or 300
        threshold = timedelta(seconds=interval * STALE_MULTIPLE)
        if last is None or (now - last) > threshold:
            stale.append(
                StaleMember(
                    member_id=str(m.id),
                    fleet_id=str(m.fleet_id),
                    host=m.host,
                    last_ping_at=last,
                    seconds_since_ping=((now - last).total_seconds() if last else None),
                    reconcile_interval_seconds=interval,
                )
            )
    return stale
