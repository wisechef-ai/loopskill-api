"""tests/test_t5734546c_liveness_ping.py — the sync-report IS the liveness writer.

t_5734546c (2026-11-27): ``fleet_member_liveness.last_ping_at`` had exactly one
writer — the owner-gated ``loopskill_ping`` MCP tool, which no client calls —
so prod sat at 2026-07-16 for all three live members while
``fleet_members.updated_at`` stayed current: ``ingest_sync_report()`` stamped it
on every collector cycle. The decision this suite pins:

  * an ACCEPTED sync-report IS the member's operational ping — the ingest path
    upserts ``last_ping_at`` in the same transaction that bumps ``updated_at``;
  * creation stamps the collector cadence (1800s) so the stale-member alert's
    3x threshold tolerates two missed */30 cycles instead of crying wolf at the
    300s model default;
  * the owner's ``provides{}`` advertisement and declared interval are NEVER
    touched by an ingest — ``loopskill_ping`` stays authoritative;
  * the ack's ``member_ping`` is derived from the actual write
    ("created" | "refreshed"), never a constant the write path cannot falsify;
  * the assign preflight's fail-closed never-pinged gate now keys on a real
    heard-from signal: one sync-report makes a member placeable, no report
    keeps it refused (dark-from-birth is still loud).

RED-proofs: each assertion maps to a mutation in this fix (stamp removed,
provides clobbered, interval defaulted, ack hard-coded).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.auth_ctx import AuthContext
from app.models import APIKey, Fleet, FleetMember, FleetMemberLiveness, User
from app.mcp.tools import fleet_ingest as ing
from app.services.placement import preflight_member
from app.services.stale_member_alert import find_stale_members
from app.services.sync_report import (
    SYNC_REPORT_RECONCILE_INTERVAL_SECONDS,
    ingest_sync_report,
)


def _mk_fleet(db, owner_id=None):
    owner_id = owner_id or uuid4()
    f = Fleet(id=uuid4(), owner_user_id=owner_id, name="f", fleet_api_key_hash=f"fh-{uuid4().hex}")
    db.add(f)
    db.flush()
    return f


def _mk_member(db, fleet, host="a"):
    u = User(id=uuid4(), display_name=host)
    db.add(u)
    db.flush()
    k = APIKey(id=uuid4(), user_id=u.id, key_prefix=uuid4().hex[:8], key_hash=f"h-{uuid4().hex}", name="k")
    db.add(k)
    db.flush()
    m = FleetMember(
        id=uuid4(), fleet_id=fleet.id, host=host, profile="default", skills_dir="~/.h", api_key_id=k.id
    )
    db.add(m)
    db.flush()
    return m


def _liveness_row(db, member):
    return db.query(FleetMemberLiveness).filter_by(member_id=member.id).first()


# ── 1. first accepted report creates the liveness row ───────────────────────


def test_first_report_creates_liveness_row(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()

    recorded, _ = ingest_sync_report(db_session, m, {})

    db_session.refresh(m)
    row = _liveness_row(db_session, m)
    assert row is not None, "a member that just reported must leave a liveness row"
    # Same instant as the updated_at marker: one report, one timestamp.
    assert row.last_ping_at == m.updated_at
    # Collector cadence, not the 300s model default.
    assert row.reconcile_interval_seconds == SYNC_REPORT_RECONCILE_INTERVAL_SECONDS
    assert SYNC_REPORT_RECONCILE_INTERVAL_SECONDS == 1800
    # An ingest must not fabricate capabilities — provides stays the owner's.
    assert row.provides == {}
    # The ack is derived from the actual write, not a constant.
    assert recorded["member_ping"] == "created"


# ── 2. refresh advances the ping, never the owner's advertisement ────────────


def test_refresh_preserves_owner_state_and_advances_ping(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.add(
        FleetMemberLiveness(
            member_id=m.id,
            last_ping_at=datetime.now(UTC) - timedelta(hours=2),
            provides={"os": "linux", "runtimes": {"python": "3.12"}},
            reconcile_interval_seconds=600,
        )
    )
    db_session.commit()
    old_ping = _liveness_row(db_session, m).last_ping_at

    recorded, _ = ingest_sync_report(db_session, m, {})

    db_session.refresh(m)
    row = _liveness_row(db_session, m)
    assert row.last_ping_at != old_ping, "a new report must advance the ping"
    assert row.last_ping_at == m.updated_at
    assert row.provides == {"os": "linux", "runtimes": {"python": "3.12"}}
    assert row.reconcile_interval_seconds == 600, "owner-declared interval is authoritative"
    assert recorded["member_ping"] == "refreshed"


def test_refresh_does_not_create_a_second_row(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()

    ingest_sync_report(db_session, m, {})
    ingest_sync_report(db_session, m, {})

    rows = db_session.query(FleetMemberLiveness).filter_by(member_id=m.id).all()
    assert len(rows) == 1, "upsert, not append"


# ── 3. assign preflight: never-pinged now keys on a real signal ──────────────


def test_sync_report_unblocks_never_pinged_preflight(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet, host="reporter")
    db_session.commit()

    # Control first: a member with no liveness row fails closed, loudly.
    before = preflight_member(db_session, m.id, "some-loop")
    assert not before.ok
    assert "liveness:member-never-pinged" in before.missing

    ingest_sync_report(db_session, m, {})

    after = preflight_member(db_session, m.id, "some-loop")
    assert "liveness:member-never-pinged" not in after.missing, (
        "a member that reported must no longer read as never-pinged"
    )


def test_member_without_any_signal_still_fails_closed(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet, host="silent")
    db_session.commit()

    result = preflight_member(db_session, m.id, "some-loop")
    assert not result.ok
    assert "liveness:member-never-pinged" in result.missing


# ── 4. the stale-member alert reads a real ping ──────────────────────────────


def test_fresh_report_ping_is_not_stale(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()

    ingest_sync_report(db_session, m, {})

    stale = [s for s in find_stale_members(db_session) if s.member_id == str(m.id)]
    assert stale == [], "a member that pinged seconds ago is not stale"


def test_ping_older_than_three_intervals_is_stale(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    ingest_sync_report(db_session, m, {})

    row = _liveness_row(db_session, m)
    row.last_ping_at = datetime.now(UTC) - timedelta(
        seconds=SYNC_REPORT_RECONCILE_INTERVAL_SECONDS * 3 + 60
    )
    db_session.commit()

    stale = [s for s in find_stale_members(db_session) if s.member_id == str(m.id)]
    assert len(stale) == 1
    assert stale[0].reconcile_interval_seconds == SYNC_REPORT_RECONCILE_INTERVAL_SECONDS


# ── 5. the owner ping stays authoritative for capabilities ───────────────────


def test_owner_ping_still_writes_provides_after_sync_report(db_session):
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    ingest_sync_report(db_session, m, {})  # member's own report-ping first

    res = ing.loopskill_ping(
        db_session,
        str(m.id),
        provides={"os": "linux", "arch": "x86_64", "runtimes": {"python": "3.12.0"}},
        ctx=AuthContext(scope="user", user_id=fleet.owner_user_id),
    )
    assert res["ok"] is True

    row = _liveness_row(db_session, m)
    assert row.provides["os"] == "linux"
    rows = db_session.query(FleetMemberLiveness).filter_by(member_id=m.id).all()
    assert len(rows) == 1, "owner ping upserts onto the report-created row"


# ── 6. ack honesty: member_ping reflects the write path ──────────────────────


def test_ack_member_ping_differs_between_create_and_refresh(db_session):
    """A hard-coded ack value cannot pass this: create != refreshed."""
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()

    first, _ = ingest_sync_report(db_session, m, {})
    second, _ = ingest_sync_report(db_session, m, {})

    assert first["member_ping"] == "created"
    assert second["member_ping"] == "refreshed"
    assert first["member_ping"] != second["member_ping"]
