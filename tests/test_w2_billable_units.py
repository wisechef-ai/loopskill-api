"""W2 — the billable-candidate counter (instrumentation for a future meter).

LoopSkill has a CAP but no METER, so usage growth produces no revenue growth.
This phase does NOT add a meter (lock #24: no price, no SKU, no Stripe usage
record). It adds the COUNTER a meter would attach to, and the property that
makes such a counter trustworthy at all: **synthetic traffic is separable.**

That property is load-bearing, not cosmetic. In production 1759 of 1760
``loop_runs`` rows are LoopSkill's own ``*/3min`` self-beacon. A usage number
that cannot exclude it is off by three orders of magnitude.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from app.billable_units import SYNTHETIC_MARKER, billable_units, current_period
from app.database import get_db
from app.models import APIKey, Fleet, FleetMember, LoopRun, Org, User


# ── Builders ────────────────────────────────────────────────────────────────


def _user(db) -> User:
    u = User(id=uuid4(), display_name="W2 Meter", email=f"w2m-{uuid4().hex[:8]}@test.example")
    db.add(u)
    db.flush()
    return u


def _org(db, name: str) -> Org:
    o = Org(id=uuid4(), name=name, slug=f"{name.lower()}-{uuid4().hex[:6]}", api_key_hash=uuid4().hex)
    db.add(o)
    db.flush()
    return o


def _fleet(db, owner: User, *, org: Org | None = None) -> Fleet:
    f = Fleet(
        id=uuid4(),
        owner_user_id=owner.id,
        name=f"fleet-{uuid4().hex[:6]}",
        fleet_api_key_hash=uuid4().hex,
        org_id=org.id if org else None,
    )
    db.add(f)
    db.flush()
    return f


def _member(db, fleet: Fleet, owner: User, *, synthetic: bool = False, active: bool = True) -> FleetMember:
    raw = f"lsk_{uuid4().hex}"
    key = APIKey(
        id=uuid4(),
        user_id=owner.id,
        key_prefix=raw[:12],
        key_hash=hashlib.sha256(raw.encode()).hexdigest(),
        name="member",
        is_active=True,
        is_test=synthetic,
    )
    db.add(key)
    db.flush()
    m = FleetMember(
        id=uuid4(),
        fleet_id=fleet.id,
        host=f"host-{uuid4().hex[:6]}",
        profile="default",
        skills_dir="~/.hermes/loopskill",
        api_key_id=key.id,
        is_active=active,
    )
    db.add(m)
    db.flush()
    return m


def _run(db, fleet: Fleet, member_id, *, created: datetime | None = None) -> LoopRun:
    r = LoopRun(
        id=uuid4(),
        member_id=member_id,
        fleet_id=fleet.id,
        loop_slug="p4-loop-proof",
        instance_key=uuid4().hex,
        outcome="pass",
    )
    db.add(r)
    db.flush()
    if created is not None:
        r.created_at = created
        db.flush()
    return r


def _by_org(report, org: Org | None):
    key = org.id if org else None
    matches = [r for r in report.orgs if r.org_id == key]
    assert matches, f"no row for org={key}: {report.orgs}"
    return matches[0]


# ── The synthetic split ─────────────────────────────────────────────────────


class TestSyntheticIsSeparable:
    def test_marker_is_the_existing_one(self):
        """Reuse ``api_keys.is_test``; do not invent a second notion of synthetic.

        The same flag already governs the public-ranking install counts
        (app/_skill_helpers.py, app/core_routes.py). Two markers would drift.
        """
        assert SYNTHETIC_MARKER == "api_keys.is_test"

    def test_synthetic_members_are_counted_separately(self, db_session):
        u = _user(db_session)
        org = _org(db_session, "WiseChef")
        fleet = _fleet(db_session, u, org=org)
        _member(db_session, fleet, u, synthetic=False)
        _member(db_session, fleet, u, synthetic=False)
        _member(db_session, fleet, u, synthetic=True)
        db_session.commit()

        row = _by_org(billable_units(db_session), org)
        assert row.active_fleet_members == 2
        assert row.active_fleet_members_synthetic == 1
        assert row.billable_candidate_members == 2

    def test_the_self_beacon_case(self, db_session):
        """The production shape: 1 external run, many internal beacon runs.

        A counter that reported 480 here would be reporting LoopSkill billing
        itself.
        """
        u = _user(db_session)
        org = _org(db_session, "WiseChef")
        fleet = _fleet(db_session, u, org=org)
        beacon = _member(db_session, fleet, u, synthetic=True)
        external = _member(db_session, fleet, u, synthetic=False)
        for _ in range(480):
            _run(db_session, fleet, beacon.id)
        _run(db_session, fleet, external.id)
        db_session.commit()

        row = _by_org(billable_units(db_session), org)
        assert row.loop_runs == 1, "the beacon leaked into the billable count"
        assert row.loop_runs_synthetic == 480
        assert row.billable_candidate_runs == 1

    def test_unattributed_runs_are_neither_billable_nor_synthetic(self, db_session):
        """A run whose member cannot be resolved is reported, not silently dropped.

        "We cannot attribute this" must not round down to "not billable" — that
        is how a real usage figure quietly becomes an undercount.
        """
        u = _user(db_session)
        org = _org(db_session, "WiseChef")
        fleet = _fleet(db_session, u, org=org)
        _run(db_session, fleet, uuid4())  # member_id points at nothing
        db_session.commit()

        row = _by_org(billable_units(db_session), org)
        assert row.loop_runs == 0
        assert row.loop_runs_synthetic == 0
        assert row.loop_runs_unattributed == 1

    def test_inactive_members_are_not_counted(self, db_session):
        u = _user(db_session)
        org = _org(db_session, "WiseChef")
        fleet = _fleet(db_session, u, org=org)
        _member(db_session, fleet, u, active=True)
        _member(db_session, fleet, u, active=False)
        db_session.commit()

        assert _by_org(billable_units(db_session), org).active_fleet_members == 1


# ── Tenant scoping ──────────────────────────────────────────────────────────


class TestOrgScoping:
    def test_two_orgs_are_reported_separately(self, db_session):
        """The whole product thesis is one operator, N isolated client fleets —
        a per-org counter that pooled them would be useless for billing either."""
        u = _user(db_session)
        wisechef, astrovita = _org(db_session, "WiseChef"), _org(db_session, "Astrovita")
        f1, f2 = _fleet(db_session, u, org=wisechef), _fleet(db_session, u, org=astrovita)
        m1 = _member(db_session, f1, u)
        _member(db_session, f2, u)
        _member(db_session, f2, u)
        _run(db_session, f1, m1.id)
        db_session.commit()

        report = billable_units(db_session)
        assert _by_org(report, wisechef).active_fleet_members == 1
        assert _by_org(report, wisechef).loop_runs == 1
        assert _by_org(report, astrovita).active_fleet_members == 2
        assert _by_org(report, astrovita).loop_runs == 0
        assert _by_org(report, wisechef).org_name == "WiseChef"

    def test_personal_scope_fleets_bucket_under_none(self, db_session):
        u = _user(db_session)
        fleet = _fleet(db_session, u, org=None)
        _member(db_session, fleet, u)
        db_session.commit()

        row = _by_org(billable_units(db_session), None)
        assert row.active_fleet_members == 1
        assert row.org_name is None

    def test_org_filter_narrows_the_report(self, db_session):
        u = _user(db_session)
        wisechef, astrovita = _org(db_session, "WiseChef"), _org(db_session, "Astrovita")
        _member(db_session, _fleet(db_session, u, org=wisechef), u)
        _member(db_session, _fleet(db_session, u, org=astrovita), u)
        db_session.commit()

        report = billable_units(db_session, org_id=wisechef.id)
        assert [r.org_id for r in report.orgs] == [wisechef.id]

    def test_org_filter_answers_zero_for_an_empty_tenant(self, db_session):
        """An explicitly-requested tenant gets a row even at zero — an empty list
        is ambiguous between "no usage" and "no such org"."""
        org = _org(db_session, "Quiet")
        db_session.commit()

        report = billable_units(db_session, org_id=org.id)
        assert len(report.orgs) == 1
        assert report.orgs[0].active_fleet_members == 0
        assert report.orgs[0].loop_runs == 0


# ── Period windowing ────────────────────────────────────────────────────────


class TestPeriod:
    def test_current_period_starts_at_the_month_boundary(self):
        now = datetime(2026, 8, 7, 13, 45, 30, tzinfo=UTC)
        start, end = current_period(now)
        assert start == datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
        assert end == now

    def test_runs_outside_the_window_are_excluded(self, db_session):
        u = _user(db_session)
        org = _org(db_session, "WiseChef")
        fleet = _fleet(db_session, u, org=org)
        m = _member(db_session, fleet, u)
        now = datetime.now(UTC)
        _run(db_session, fleet, m.id, created=now - timedelta(days=1))
        _run(db_session, fleet, m.id, created=now - timedelta(days=40))
        db_session.commit()

        report = billable_units(
            db_session, period_start=now - timedelta(days=7), period_end=now + timedelta(minutes=1)
        )
        assert _by_org(report, org).loop_runs == 1

    def test_window_is_half_open(self, db_session):
        """[start, end) — a run exactly at `end` belongs to the NEXT period, so
        two adjacent periods can never double-count the same run."""
        u = _user(db_session)
        org = _org(db_session, "WiseChef")
        fleet = _fleet(db_session, u, org=org)
        m = _member(db_session, fleet, u)
        boundary = datetime.now(UTC).replace(microsecond=0)
        _run(db_session, fleet, m.id, created=boundary)
        db_session.commit()

        assert _by_org(
            billable_units(db_session, period_start=boundary, period_end=boundary + timedelta(seconds=1)),
            org,
        ).loop_runs == 1
        assert _by_org(
            billable_units(db_session, period_start=boundary - timedelta(seconds=1), period_end=boundary),
            org,
        ).loop_runs == 0


# ── Exposure on the pulse surface ───────────────────────────────────────────


def _pulse_app(db, *, is_admin: bool):
    from app.admin_routes import router as admin_router

    app = FastAPI()

    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = None if is_admin else uuid4()
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.include_router(admin_router)
    return app


class TestPulseExposesBillableUnits:
    def test_pulse_reports_per_org_counts_with_synthetic_split(self, db_session):
        u = _user(db_session)
        org = _org(db_session, "WiseChef")
        fleet = _fleet(db_session, u, org=org)
        beacon = _member(db_session, fleet, u, synthetic=True)
        real = _member(db_session, fleet, u, synthetic=False)
        _run(db_session, fleet, beacon.id)
        _run(db_session, fleet, beacon.id)
        _run(db_session, fleet, real.id)
        db_session.commit()

        with TestClient(_pulse_app(db_session, is_admin=True)) as client:
            r = client.get("/api/admin/pulse")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "billable_units" in body
        assert body["billable_units_period_start"] <= body["billable_units_period_end"]

        row = next(x for x in body["billable_units"] if x["org_id"] == str(org.id))
        assert row["org_name"] == "WiseChef"
        assert row["active_fleet_members"] == 1
        assert row["active_fleet_members_synthetic"] == 1
        assert row["loop_runs"] == 1
        assert row["loop_runs_synthetic"] == 2

    def test_pulse_org_id_filter_applies(self, db_session):
        """Verify the SIDE EFFECT, not the 200 — FastAPI silently ignores an
        unknown query param, so a passing status code proves nothing (trap E2)."""
        u = _user(db_session)
        wisechef, astrovita = _org(db_session, "WiseChef"), _org(db_session, "Astrovita")
        _member(db_session, _fleet(db_session, u, org=wisechef), u)
        _member(db_session, _fleet(db_session, u, org=astrovita), u)
        db_session.commit()

        with TestClient(_pulse_app(db_session, is_admin=True)) as client:
            unfiltered = client.get("/api/admin/pulse").json()
            filtered = client.get(f"/api/admin/pulse?org_id={wisechef.id}").json()

        assert {x["org_id"] for x in unfiltered["billable_units"]} >= {
            str(wisechef.id),
            str(astrovita.id),
        }
        assert [x["org_id"] for x in filtered["billable_units"]] == [str(wisechef.id)]

    def test_pulse_billable_units_still_master_key_only(self, db_session):
        with TestClient(_pulse_app(db_session, is_admin=False)) as client:
            assert client.get("/api/admin/pulse").status_code == 403

    def test_no_price_or_usage_record_is_introduced(self):
        """Lock #24: this phase adds instrumentation, never billing.

        Guards the module against a later edit that turns the counter into a
        charge without the pricing decision being made explicitly.
        """
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "app" / "billable_units.py").read_text()
        for forbidden in ("SubscriptionItem", "UsageRecord", "create_usage_record", "stripe."):
            assert forbidden not in src, f"billable_units.py reaches into billing: {forbidden}"
