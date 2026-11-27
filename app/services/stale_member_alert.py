"""fleetos_1607 Phase A — stale-member alert (the failover replacement).

§0 #16a deleted Phase F failover. Its operator outcome — "a host went dark, do
something" — is delivered instead by a detector that flags members whose
operational ping is older than a multiple of their reconcile interval, plus a
MANUAL evacuate the operator runs. Alert only; no automatic reassignment. The
trust ledger watches this; auto-failover earns its way in later once the ledger
has data to justify it.

CALLERS (t_e5dcc325, 2026-11-27) — this module used to promise the sentence
above while nothing in ``app/`` called it: only tests imported
:func:`find_stale_members`, so "a host went dark" could never reach an
on-call engineer, which is the same silent-death class as the ``prune_raw`` (no caller
for 4½ months) and ``skill_errors`` retirements. It now has two production
callers, and ``tests/test_t_e5dcc325_stale_member_caller.py`` fails if either
goes missing again:

* ``app/crons/stale_member_alert.py`` — the scheduled ops sweep (the push
  path; installed on wisechef-hq via ``deploy/crontab.stale-member-alert``).
* ``app/dashboard_routes.py`` — ``stale_members`` in the fleet-dashboard
  payload (the pull path; ``GET /api/fleets/{fleet_id}/dashboard`` is the only
  fleet surface with live traffic — 10 organic hits/30d, 23 hits in the 7 days
  before this shipped, while every other ``/api/fleets/*`` route sits tombstoned
  at 0).

TENANT SCOPE: pass ``fleet_id`` from anything serving a single tenant. Called
unscoped this returns members of EVERY fleet in the deployment, which is
correct for the deployment-wide sweep and a cross-tenant leak in a per-fleet response
(``astrovita``'s hostnames arriving in another client's dashboard) — the exact
defect codex caught in ``app/mcp/bundle_status.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import FleetMember, FleetMemberLiveness

# A member is "stale" when its last ping is older than this multiple of its
# declared reconcile interval. 3× tolerates one or two missed cycles before
# crying wolf (a single slow tick is not a dead host).
STALE_MULTIPLE = 3

# What a member with NO liveness row is reported against. It has no declared
# interval to compare to — the row does not exist — so the alert's own floor is
# reported for display, and the staleness itself is unconditional (a member that
# has never pinged is dark by definition, whatever interval it would have
# declared). Mirrors the model default on FleetMemberLiveness.
NEVER_PINGED_INTERVAL_SECONDS = 300


@dataclass
class StaleMember:
    member_id: str
    fleet_id: str
    host: str
    last_ping_at: datetime | None
    seconds_since_ping: float | None
    reconcile_interval_seconds: int

    @property
    def never_pinged(self) -> bool:
        """True when this member has never produced a liveness ping at all.

        Distinguishes "enrolled and silent since birth" (a provisioning gap —
        the member was never wired to report) from "went quiet" (a host that
        used to check in and stopped). They are different responses: the
        first is a setup bug, the second is the dark host this alert exists for.
        """
        return self.last_ping_at is None

    def payload(self) -> dict[str, Any]:
        """ONE serializer for both lanes (cron sample JSON + dashboard body).

        Two hand-built dicts drifted on day one (the pull lane silently dropped
        ``fleet_id``) — ocr r2 finding. Both callers go through here so any new
        field lands in push AND pull, never one.
        """
        return {
            "member_id": self.member_id,
            "fleet_id": self.fleet_id,
            "host": self.host,
            "last_ping_at": self.last_ping_at.isoformat() if self.last_ping_at else None,
            "seconds_since_ping": self.seconds_since_ping,
            "reconcile_interval_seconds": self.reconcile_interval_seconds,
            "never_pinged": self.never_pinged,
        }


def by_urgency(stale: list[StaleMember]) -> list[StaleMember]:
    """Deterministic, urgency-ordered view shared by both callers.

    ``_active_member_query`` has no ORDER BY, so DB order is undefined: without
    this the most-overdue host can be buried below a barely-late one, and the
    cron's SAMPLE_LIMIT would silently sample the wrong 25. Never-pinged first
    (an enrolled-and-silent member is the provisioning gap the alert exists
    for), then oldest ping. One definition, so the log sample and the dashboard
    badge cannot disagree about what the reader should look at first.
    """
    return sorted(stale, key=lambda s: (not s.never_pinged, -(s.seconds_since_ping or 0.0)))


def _fleet_scope(fleet_id: UUID | str | None) -> tuple[UUID | None, bool]:
    """Coerce a caller-supplied fleet id. Returns ``(uuid, unparseable)``.

    ``unparseable`` means the value was supplied but is not a UUID: callers must
    then match NOTHING. Falling back to unscoped there would answer a malformed
    tenant request with the whole deployment.
    """
    if fleet_id is None:
        return None, False
    if isinstance(fleet_id, UUID):
        return fleet_id, False
    try:
        return UUID(str(fleet_id)), False
    except (ValueError, AttributeError, TypeError):
        return None, True


def _active_member_query(db: Session, fleet_uuid: UUID | None):
    """THE active-member filter. Both the sweep and the denominator go through
    here so the population a caller counts cannot drift from the population the
    detector examined."""
    q = db.query(FleetMember).filter(FleetMember.is_active == True)  # noqa: E712
    if fleet_uuid is not None:
        q = q.filter(FleetMember.fleet_id == fleet_uuid)
    return q


def count_active_members(db: Session, *, fleet_id: UUID | str | None = None) -> int:
    """How many active members a sweep of this scope actually examined.

    The denominator is what makes an all-clear falsifiable. ``stale_count == 0``
    over 47 members means "everyone is alive"; over 0 members means the sweeper
    reached the wrong database, or a ``--fleet-id`` was typo'd. Without it a
    caller can only assert ``len() == 0``, and a green light that cannot be
    sanity-checked is how the last alert died.
    """
    fleet_uuid, unparseable = _fleet_scope(fleet_id)
    if unparseable:
        return 0
    return _active_member_query(db, fleet_uuid).count()


def find_stale_members(
    db: Session,
    now: datetime | None = None,
    *,
    fleet_id: UUID | str | None = None,
) -> list[StaleMember]:
    """Return active members whose liveness ping is older than STALE_MULTIPLE×interval.

    A member that has never pinged (no liveness row) but is active is also
    surfaced — an enrolled-but-silent member is exactly the dark-host case the
    alert exists for.

    ``fleet_id`` narrows the sweep to one fleet and MUST be passed by any
    per-tenant caller (see the module docstring — unscoped is cross-tenant).
    ``None`` means every active member in the deployment, which is what the
    deployment-wide cron sweep wants.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    fleet_uuid, unparseable = _fleet_scope(fleet_id)
    if unparseable:
        # An unparseable fleet id scopes to "no fleet at all", never to
        # "every fleet" — failing open here would answer a malformed
        # tenant request with the whole deployment.
        return []

    active_members = _active_member_query(db, fleet_uuid).all()

    liveness_q = db.query(FleetMemberLiveness)
    if fleet_uuid is not None:
        # Scope the join too: without this the liveness table is read in full
        # on every per-fleet dashboard hit, which is both waste and a needless
        # cross-tenant read of rows the caller may not see.
        liveness_q = liveness_q.filter(
            FleetMemberLiveness.member_id.in_(
                db.query(FleetMember.id).filter(FleetMember.fleet_id == fleet_uuid)
            )
        )
    liveness_by_member = {lv.member_id: lv for lv in liveness_q.all()}

    stale: list[StaleMember] = []
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
                    reconcile_interval_seconds=NEVER_PINGED_INTERVAL_SECONDS,
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
