"""agentreg_0819 (review round 4) — lock-and-count trailing-window quota.

WHY ROUND 4 REPLACED ROUND 3
----------------------------
Round 3 tried to keep round 2's counter rows and add sliding-window semantics
on top: two fixed 12h buckets, a Python-side read of the previous bucket, and
a single conditional UPDATE on the current one. The final adversarial review
proved that design broken in two independent ways:

1. CROSS-BOUNDARY RACE. A transaction that began before the bucket boundary
   still updates the now-previous row; the new bucket's transaction embedded
   a stale Python-side read of it as a constant. Result: count(A)+count(B)
   could reach 2x cap.
2. ALTERNATE-BOUNDARY BYPASS. Any fixed-bucket pair forgets a full bucket
   after two windows: cap at the end of A + cap at the start of C never share
   a pair, so 2x cap fits inside a true trailing 24h.

The lesson: mixing an immutable-counters design with rolling-window semantics
buys the disadvantages of both.

THE ROUND-4 DESIGN — LOCK, THEN COUNT
-------------------------------------
A serialisation GATE row per scope (global, or one IP) is locked with
``SELECT ... FOR UPDATE`` (no-op on SQLite, whole-file serialisation there —
see below). With every concurrent reserver of that scope serialised, the code
then COUNTS the rows that actually exist in the exact trailing 24h and
refuses at >= cap. No buckets, no boundaries, no shares, no embedded
constants — the invariant checked is the invariant that must hold.

THE SQLITE QUESTION
-------------------
``FOR UPDATE`` parses as a no-op on SQLite. That is safe HERE (and was not
for round 2's counter design) because SQLite serialises every write
transaction against the whole database file: two concurrent registrations
cannot interleave at all. The count-then-insert window that round 1's race
exploited is impossible on SQLite by engine semantics, and closed on Postgres
by the gate row's lock. One implementation, correct on both engines, no
dialect branch.

VOLUME ARGUMENT
----------------
The gate serialises concurrent registrations within one scope. At the
configured caps (3/IP/day, 20/day global) contention is unmeasurable: 20
registrations spread over a day cannot queue on one row lock for a
meaningful instant. The design trades a lock for correctness at exactly the
volume where a lock is free.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AgentIdentity, AgentRegistrationGate

WINDOW = timedelta(hours=24)

GLOBAL_SCOPE = "global"


def gate_scope_for_ip(client_ip: str) -> str:
    """The gate scope serialising all registrations from one source address."""
    return f"ip:{client_ip}"


def _acquire_gate(db: Session, *, scope: str, now: datetime) -> None:
    """Serialise concurrent reservers of ``scope`` until the caller commits.

    Creates the gate row if absent (SAVEPOINT so the losing side of a
    create-race rolls back only the insert), then takes the lock with a real
    WRITE (see the comment at the UPDATE below). With every concurrent
    reserver of that scope serialised, the trailing-24h count that follows
    cannot interleave with a rival's count-then-insert on either engine.
    """
    existing = db.query(AgentRegistrationGate).filter(AgentRegistrationGate.scope == scope).first()
    if existing is None:
        try:
            with db.begin_nested():
                db.add(AgentRegistrationGate(scope=scope))
                db.flush()
        except IntegrityError:
            # Concurrent create: the row now exists — proceed to lock it.
            pass
    # The lock is an UPDATE — a real WRITE — not SELECT ... FOR UPDATE,
    # because FOR UPDATE is a silent no-op on SQLite (the concurrency test
    # runs there). The write holds the Postgres row lock / SQLite database
    # write lock from this statement until the caller's COMMIT, so the
    # trailing-24h count that follows cannot interleave with a rival's
    # count-then-insert on either engine.
    db.execute(
        update(AgentRegistrationGate)
        .where(AgentRegistrationGate.scope == scope)
        .values(last_reserved_at=now)
        .execution_options(synchronize_session=False)
    )


def _trailing_count(db: Session, *, scope: str, now: datetime) -> int:
    """Registrations that actually exist in the exact trailing 24h.

    Called ONLY while holding the scope's gate lock, so the value cannot
    change between this read and the caller's insert (on Postgres; on SQLite
    engine-level serialisation provides the same guarantee).
    """
    horizon = now - WINDOW
    query = db.query(AgentIdentity).filter(AgentIdentity.created_at >= horizon)
    if scope != GLOBAL_SCOPE:
        query = query.filter(AgentIdentity.registration_ip == scope.removeprefix("ip:"))
    return int(query.count())


def reserve_registration_slot(
    db: Session,
    *,
    scope: str,
    cap: int,
    now: datetime,
) -> bool:
    """Take one trailing-24h slot for ``scope``, or refuse — atomically per scope.

    The caller MUST be inside the transaction that will insert the
    AgentIdentity (round 1's defect was deciding in one transaction and
    writing in another). Gate lock is taken here; count against the real
    table follows; the caller's later insert lands under the same lock and
    the caller's rollback releases both.
    """
    if cap <= 0:
        return False
    _acquire_gate(db, scope=scope, now=now)
    return _trailing_count(db, scope=scope, now=now) < cap


def seconds_until_capacity(db: Session, *, scope: str, now: datetime) -> int:
    """Seconds until a slot is GUARANTEED to exist for ``scope`` (N3, honest).

    The oldest registration inside the trailing window expires out of it
    exactly 24h after it was created; that is the first instant the count
    provably drops. Reads the oldest in-window row under NO lock — this is
    advisory header math, not a decision (the decision is ``reserve_registration_slot``).
    Rounded up to the next whole minute, never below 60. If the table is
    empty the scope is not capped and 60 is returned.
    """
    horizon = now - WINDOW
    query = (
        db.query(AgentIdentity)
        .filter(AgentIdentity.created_at >= horizon)
        .order_by(AgentIdentity.created_at.asc())
    )
    if scope != GLOBAL_SCOPE:
        query = query.filter(AgentIdentity.registration_ip == scope.removeprefix("ip:"))
    oldest = query.first()
    if oldest is None or oldest.created_at is None:
        return 60
    aware = oldest.created_at
    if aware.tzinfo is None:
        aware = aware.replace(tzinfo=UTC)
    capacity_at = aware + WINDOW
    delta = (capacity_at - now).total_seconds()
    return max(60, int(delta // 60 * 60) + (60 if delta % 60 else 0))
