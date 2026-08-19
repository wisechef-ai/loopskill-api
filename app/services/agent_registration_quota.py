"""agentreg_0819 (review round 2, F1) — the ATOMIC enrolment-quota reservation.

WHAT WAS WRONG
--------------
Round 1 enforced the per-IP and platform-wide enrolment caps like this::

    count = db.query(AgentIdentity).filter(...).count()   # read
    if count >= cap:                                      # decide
        raise 429
    ...                                                   # write, much later

That is a textbook check-then-act. Every request that arrives while the count
sits at ``cap - 1`` reads ``cap - 1``, decides "under cap", and commits. Fire
50 signed registrations concurrently at a cap of 3 and all 50 land — so the
cap bounded a *sequential* attacker and nothing else, which is the attacker who
was never the threat. Since this endpoint mints a real API key with no human in
the loop, the cap is the whole abuse wall.

THE FIX: RESERVE, DON'T COUNT
-----------------------------
The count moves out of an aggregate over ``agent_identities`` and into a
dedicated counter ROW per (bucket, window) — ``AgentRegistrationQuota``. A slot
is then taken with ONE statement that reads and writes in the same breath::

    UPDATE agent_registration_quota
       SET count = count + 1
     WHERE bucket = :b AND window_start = :w AND count < :cap

``rowcount == 1`` means the slot is ours; ``rowcount == 0`` means the row was
already at the cap. There is no window between the decision and the write
because they are the same operation, and the guard is re-evaluated by the
database against the row's committed value, not against a value Python read
earlier.

WHY THIS IS ATOMIC ON POSTGRES
------------------------------
Production is Postgres at READ COMMITTED. Two concurrent transactions issuing
that UPDATE contend on the row: the second blocks on the first's row lock, and
when the first commits, Postgres RE-EVALUATES the second's WHERE clause against
the newly committed row (the documented EvalPlanQual re-check for UPDATE). So
the loser sees the incremented ``count`` and matches zero rows. No
``SELECT ... FOR UPDATE`` is needed — the UPDATE takes the same row lock, and
folding the guard into it removes the gap a separate SELECT would reopen.

WHY IT IS ALSO CORRECT ON SQLITE
--------------------------------
The test suite runs SQLite, where ``FOR UPDATE`` parses as a no-op and would
have silently bought nothing — which is exactly why the guard lives in the
UPDATE's WHERE clause instead. SQLite serialises write transactions against the
whole database file, so the two statements cannot interleave at all and the
same invariant holds for a stricter reason. One implementation, no dialect
branch, and nothing that is a real lock on one engine and a comment on the
other.

RELEASE ON FAILURE IS FREE
--------------------------
The reservation runs inside the caller's transaction. If the mint later fails
(duplicate pubkey losing the UNIQUE race, say), the caller's ``rollback()``
takes the increment with it. A refused registration therefore cannot consume a
legitimate agent's allowance — the property the 409 path in
``app.services.agent_registration`` depends on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AgentRegistrationQuota

GLOBAL_BUCKET = "global"


def ip_bucket(client_ip: str) -> str:
    """The counter bucket for one source address."""
    return f"ip:{client_ip}"


def window_start_for(now: datetime) -> datetime:
    """Floor ``now`` to the start of its UTC day — the quota window.

    Fixed windows, not rolling ones: a rolling edge has no row to lock, and a
    cap you cannot lock is a cap you cannot enforce concurrently. See the
    ``AgentRegistrationQuota`` docstring for the (bounded) trade this accepts.
    """
    aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class Reservation:
    """The outcome of one reservation attempt."""

    granted: bool
    bucket: str
    cap: int


def _ensure_counter_row(db: Session, *, bucket: str, window_start: datetime) -> None:
    """Create the (bucket, window) counter row if it does not exist yet.

    Runs in a SAVEPOINT so the losing side of a first-registration-of-the-day
    race rolls back only this INSERT and leaves the caller's transaction
    intact. Portable across Postgres and SQLite — an ``ON CONFLICT DO NOTHING``
    would need a dialect branch to say the same thing.
    """
    existing = (
        db.query(AgentRegistrationQuota)
        .filter(
            AgentRegistrationQuota.bucket == bucket,
            AgentRegistrationQuota.window_start == window_start,
        )
        .first()
    )
    if existing is not None:
        return
    try:
        with db.begin_nested():
            db.add(AgentRegistrationQuota(bucket=bucket, window_start=window_start, count=0))
            db.flush()
    except IntegrityError:
        # Someone else created it first. That is the desired end state.
        pass


def reserve_registration_slot(
    db: Session,
    *,
    bucket: str,
    cap: int,
    now: datetime,
) -> Reservation:
    """Atomically take one slot from ``bucket``'s current window, or refuse.

    THE SINGLE CONDITIONAL UPDATE. This function must emit exactly one
    read-modify-write statement for the decision — ``count = count + 1`` guarded
    by ``count < cap`` in the same WHERE clause. Any refactor that splits it
    into a SELECT followed by an UPDATE reintroduces the race this exists to
    close; ``tests/test_agentreg_0819_agent_self_registration.py`` asserts the
    emitted SQL structurally so such a refactor fails the suite rather than
    passing review.

    A non-positive ``cap`` refuses without touching the database — no UPDATE
    can be satisfied by ``count < 0``, and materialising a counter row for a
    disabled bucket is pointless.
    """
    if cap <= 0:
        return Reservation(granted=False, bucket=bucket, cap=cap)

    window_start = window_start_for(now)
    _ensure_counter_row(db, bucket=bucket, window_start=window_start)

    stmt = (
        update(AgentRegistrationQuota)
        .where(
            AgentRegistrationQuota.bucket == bucket,
            AgentRegistrationQuota.window_start == window_start,
            # THE GUARD. It lives here, inside the write, and nowhere else.
            AgentRegistrationQuota.count < cap,
        )
        .values(count=AgentRegistrationQuota.count + 1)
        .execution_options(synchronize_session=False)
    )
    result = db.execute(stmt)
    return Reservation(granted=bool(result.rowcount == 1), bucket=bucket, cap=cap)


def current_usage(db: Session, *, bucket: str, now: datetime) -> int:
    """Slots taken in ``bucket``'s current window. Diagnostics/tests only.

    Never call this to DECIDE anything — reading the counter and then acting on
    what you read is the exact defect ``reserve_registration_slot`` removes.
    """
    row = (
        db.query(AgentRegistrationQuota)
        .filter(
            AgentRegistrationQuota.bucket == bucket,
            AgentRegistrationQuota.window_start == window_start_for(now),
        )
        .first()
    )
    return int(row.count) if row is not None else 0
