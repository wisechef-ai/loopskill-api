"""mesh_0408 W4 — synthetic (self-originated) vs external loop runs.

THE NUMBER THIS EXISTS TO STOP:

    loop_runs = 1760  ->  p4-loop-proof: 1759   (LoopSkill's own */3min beacon)
                          atomic-habits:    1
    Last 24h: 486 runs, ONE member, ONE loop.

Every surface reported ``1760`` and called it usage. Lock #25 makes D-006's
monetisation thesis conditional on loops actually running; a self-beacon
counted as adoption is how a product lies to the person deciding what to build
next. So: the two numbers must be separable everywhere, and no surface may
report the combined figure on its own.

``TestASyntheticRunCanNeverBeCountedAsExternal`` is the gate the phase brief
requires — it fails if a synthetic run reaches an external count by ANY of the
four routes (member flag, fleet flag, key ``is_test``, known beacon slug).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from app.models import APIKey, Fleet, FleetMember, LoopRun, LoopRunDailyRollup, User
from app.services import run_registry
from app.services.sync_report import ingest_sync_report, rollup_loop_runs
from app.services.synthetic_runs import (
    SELF_ORIGINATED_LOOP_SLUGS,
    RunCounts,
    classify_run_synthetic,
    member_is_synthetic,
    set_fleet_origin,
    set_member_origin,
)

BEACON_SLUG = "p4-loop-proof"
EXTERNAL_SLUG = "atomic-habits"


def _mk_fleet(db, *, is_synthetic=None):
    """``is_synthetic=None`` = UNCLASSIFIED — nobody has said, which is the
    state every pre-W4 row is in and the only state the slug backstop covers."""
    f = Fleet(
        id=uuid4(),
        owner_user_id=uuid4(),
        name="f",
        fleet_api_key_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        is_synthetic=is_synthetic,
    )
    db.add(f)
    db.flush()
    return f


def _mk_member(db, fleet, *, host="h", is_synthetic=None, key_is_test=False):
    u = User(id=uuid4(), display_name="u")
    db.add(u)
    db.flush()
    k = APIKey(
        id=uuid4(),
        user_id=u.id,
        key_prefix=uuid4().hex[:8],
        key_hash=f"h-{uuid4().hex}",
        name="k",
        is_test=key_is_test,
    )
    db.add(k)
    db.flush()
    m = FleetMember(
        id=uuid4(),
        fleet_id=fleet.id,
        host=host,
        profile="default",
        skills_dir="~/.h",
        api_key_id=k.id,
        is_synthetic=is_synthetic,
    )
    db.add(m)
    db.flush()
    return m


# ── the marker itself ───────────────────────────────────────────────────────


class TestTheMarkerIsIdentityLevelNotSlugLevel:
    """The slug list is a backstop, NOT the definition. Each of the three
    identity flags must classify on its own, so a second internal beacon under
    a different name is covered without a code change."""

    def test_member_flag_marks_the_member_synthetic(self, db_session):
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet, is_synthetic=True)
        db_session.commit()
        assert member_is_synthetic(db_session, m) is True
        assert classify_run_synthetic(db_session, loop_slug="anything-at-all", member=m) is True

    def test_fleet_flag_marks_every_member_under_it(self, db_session):
        fleet = _mk_fleet(db_session, is_synthetic=True)
        m = _mk_member(db_session, fleet)  # member itself NOT classified
        db_session.commit()
        assert m.is_synthetic is None
        assert member_is_synthetic(db_session, m) is True

    def test_api_key_is_test_marks_the_member(self, db_session):
        """Follows the spotify_0608/B precedent (APIKey.is_test) rather than
        inventing a fourth vocabulary for the same idea."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet, key_is_test=True)
        db_session.commit()
        assert m.is_synthetic is None
        assert fleet.is_synthetic is None
        assert member_is_synthetic(db_session, m) is True

    def test_an_explicit_member_verdict_overrides_the_fleets(self, db_session):
        """Specificity order: the per-agent marker is the most specific claim
        anyone can make, so a real customer agent inside an internal harness
        fleet is not swallowed by the fleet-level flag."""
        fleet = _mk_fleet(db_session, is_synthetic=True)
        m = _mk_member(db_session, fleet, is_synthetic=False)
        db_session.commit()
        assert member_is_synthetic(db_session, m) is False
        assert classify_run_synthetic(db_session, loop_slug=BEACON_SLUG, member=m) is False

    def test_an_unflagged_member_running_an_unknown_loop_is_external(self, db_session):
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        assert member_is_synthetic(db_session, m) is False
        assert classify_run_synthetic(db_session, loop_slug=EXTERNAL_SLUG, member=m) is False

    def test_the_known_beacon_slug_is_a_backstop_for_unflagged_fleets(self, db_session):
        """So the number is right on day one, before anybody flags anything."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        assert BEACON_SLUG in SELF_ORIGINATED_LOOP_SLUGS
        assert classify_run_synthetic(db_session, loop_slug=BEACON_SLUG, member=m) is True


class TestTheSlugSetIsABackstopNotTheDefinition:
    """The backstop must lose to an identity that has actually been classified.

    Before the lifecycle existed, ``is_synthetic`` was never set by ANY creation
    path, so classification collapsed to the hard-coded slug set and was wrong
    in BOTH directions: a real customer who named a loop ``p4-loop-proof`` was
    silently counted as ours (under-stating adoption), and a second internal
    beacon under any other name was counted external (over-stating it — the
    exact number this phase exists to stop inflating).
    """

    def test_a_customers_loop_named_like_our_beacon_is_still_external(self, db_session):
        """The collision case. ``loop_slug`` gets only length/non-empty
        validation at ingest (app/services/fleet_ingest.py), so any customer
        can declare this name — deliberately or by copying our own docs."""
        fleet = _mk_fleet(db_session, is_synthetic=False)  # explicitly a customer's
        m = _mk_member(db_session, fleet)
        db_session.commit()

        assert classify_run_synthetic(db_session, loop_slug=BEACON_SLUG, member=m) is False

        ingest_sync_report(
            db_session,
            m,
            {"loop_runs": [{"loop_slug": BEACON_SLUG, "instance_key": uuid4().hex, "outcome": "success"}]},
        )
        state = run_registry.fleet_state(db_session, fleet.id)
        assert state["runs"] == {"total": 1, "synthetic": 0, "external": 1}, (
            "a customer's run was swallowed into our own beacon count by a slug collision"
        )

    def test_a_second_internal_beacon_under_a_new_slug_is_still_ours(self, db_session):
        """The other direction, and the reason the flag has to be settable:
        classifying the IDENTITY covers every loop it will ever run, including
        ones that do not exist yet."""
        fleet = _mk_fleet(db_session, is_synthetic=True)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        ingest_sync_report(
            db_session,
            m,
            {"loop_runs": [{"loop_slug": "p9-new-beacon", "instance_key": uuid4().hex, "outcome": "success"}]},
        )
        state = run_registry.fleet_state(db_session, fleet.id)
        assert state["runs"] == {"total": 1, "synthetic": 1, "external": 0}


class TestTheMarkersHaveALifecycle:
    """A marker no code path can set is not a marker. These cover both ends:
    stamped at creation from ``APIKey.is_test`` (the single definition), and
    settable afterwards — repairing the history frozen at ingest."""

    def test_enrolling_a_member_with_a_test_key_classifies_it_at_creation(self, db_session, monkeypatch):
        """HTTP level, through the real enrollment route."""
        from fastapi.testclient import TestClient

        from app.api_key_routes import _generate_key
        from tests._app_factory import build_test_app

        u = User(email=f"{uuid4().hex}@t.com", display_name="C", subscription_tier="pro")
        db_session.add(u)
        db_session.flush()
        pt, pfx, hs = _generate_key()
        db_session.add(
            APIKey(id=uuid4(), user_id=u.id, key_prefix=pfx, key_hash=hs, is_active=True, is_test=True)
        )
        fleet = Fleet(
            id=uuid4(),
            owner_user_id=u.id,
            name="ci",
            fleet_api_key_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        )
        db_session.add(fleet)
        db_session.commit()

        client = TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))
        r = client.post(
            f"/api/fleets/{fleet.id}/members",
            headers={"x-api-key": pt},
            json={"host": "ci-runner", "profile": "default", "skills_dir": "~/.h"},
        )
        assert r.status_code == 201, r.text

        member = db_session.query(FleetMember).filter(FleetMember.id == UUID(r.json()["member_id"])).one()
        assert member.is_synthetic is True, "a member enrolled by a test key must be classified as ours"
        # The minted per-agent key carries the same verdict, so the single
        # definition survives every later read that goes via the key.
        minted = db_session.query(APIKey).filter(APIKey.id == member.api_key_id).one()
        assert minted.is_test is True
        assert classify_run_synthetic(db_session, loop_slug=EXTERNAL_SLUG, member=member) is True

    def test_enrolling_with_a_normal_key_classifies_the_member_external(self, db_session, monkeypatch):
        """The control. Without it the test above passes on a stuck True."""
        from fastapi.testclient import TestClient

        from app.api_key_routes import _generate_key
        from tests._app_factory import build_test_app

        u = User(email=f"{uuid4().hex}@t.com", display_name="C", subscription_tier="pro")
        db_session.add(u)
        db_session.flush()
        pt, pfx, hs = _generate_key()
        db_session.add(APIKey(id=uuid4(), user_id=u.id, key_prefix=pfx, key_hash=hs, is_active=True))
        fleet = Fleet(
            id=uuid4(),
            owner_user_id=u.id,
            name="real",
            fleet_api_key_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        )
        db_session.add(fleet)
        db_session.commit()

        client = TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))
        r = client.post(
            f"/api/fleets/{fleet.id}/members",
            headers={"x-api-key": pt},
            json={"host": "customer-box", "profile": "default", "skills_dir": "~/.h"},
        )
        assert r.status_code == 201, r.text
        member = db_session.query(FleetMember).filter(FleetMember.id == UUID(r.json()["member_id"])).one()
        assert member.is_synthetic is False
        # And the explicit verdict beats the slug backstop — this is the
        # customer-collision guard applied at the creation path.
        assert classify_run_synthetic(db_session, loop_slug=BEACON_SLUG, member=member) is False

    def test_creating_a_fleet_with_a_test_key_classifies_it_at_creation(self, db_session):
        from app.auth_ctx import AuthContext
        from app.mcp.tools.fleet import loopskill_fleet_create

        u = User(id=uuid4(), display_name="u")
        db_session.add(u)
        db_session.flush()
        test_key = APIKey(
            id=uuid4(), user_id=u.id, key_prefix=uuid4().hex[:8], key_hash=f"h-{uuid4().hex}", is_test=True
        )
        real_key = APIKey(
            id=uuid4(), user_id=u.id, key_prefix=uuid4().hex[:8], key_hash=f"h-{uuid4().hex}"
        )
        db_session.add_all([test_key, real_key])
        db_session.commit()

        out = loopskill_fleet_create(
            db_session, name="ci", ctx=AuthContext(scope="user", user_id=u.id, api_key_id=test_key.id)
        )
        assert db_session.query(Fleet).filter(Fleet.id == UUID(out["fleet_id"])).one().is_synthetic is True

        out = loopskill_fleet_create(
            db_session, name="real", ctx=AuthContext(scope="user", user_id=u.id, api_key_id=real_key.id)
        )
        assert db_session.query(Fleet).filter(Fleet.id == UUID(out["fleet_id"])).one().is_synthetic is False

    def test_flagging_a_member_later_repairs_the_runs_already_ingested(self, db_session):
        """Late-flag drift. Classification is FROZEN onto the run at ingest —
        a run is an immutable fact and no read path should carry a three-table
        join. That is right, and it means flipping the flag afterwards has to
        repair the history explicitly, or the number stays wrong forever."""
        fleet = _mk_fleet(db_session)
        beacon = _mk_member(db_session, fleet, host="beacon-host")
        db_session.commit()
        ingest_sync_report(
            db_session,
            beacon,
            {
                "loop_runs": [
                    {"loop_slug": "p9-new-beacon", "instance_key": uuid4().hex, "outcome": "success"},
                    {"loop_slug": "p9-new-beacon", "instance_key": uuid4().hex, "outcome": "success"},
                ]
            },
        )
        rollup_loop_runs(db_session, day=date.today())
        assert run_registry.fleet_state(db_session, fleet.id)["runs"]["external"] == 2

        set_member_origin(db_session, beacon, synthetic=True)

        assert beacon.is_synthetic is True
        state = run_registry.fleet_state(db_session, fleet.id)
        assert state["runs"] == {"total": 2, "synthetic": 2, "external": 0}, (
            "flipping the marker left the already-ingested runs counted as adoption"
        )
        rows = (
            db_session.query(LoopRunDailyRollup)
            .filter(LoopRunDailyRollup.member_id == beacon.id)
            .all()
        )
        assert [r.synthetic_runs for r in rows] == [2], "the rollup kept the pre-flag verdict"

    def test_unflagging_repairs_in_the_other_direction_too(self, db_session):
        """A misclassification that under-states adoption must be as
        repairable as one that over-states it, or the marker is a trapdoor."""
        fleet = _mk_fleet(db_session, is_synthetic=True)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        ingest_sync_report(
            db_session,
            m,
            {"loop_runs": [{"loop_slug": EXTERNAL_SLUG, "instance_key": uuid4().hex, "outcome": "success"}]},
        )
        rollup_loop_runs(db_session, day=date.today())
        assert run_registry.fleet_state(db_session, fleet.id)["runs"]["synthetic"] == 1

        set_fleet_origin(db_session, fleet, synthetic=False)

        assert fleet.is_synthetic is False
        assert run_registry.fleet_state(db_session, fleet.id)["runs"] == {
            "total": 1,
            "synthetic": 0,
            "external": 1,
        }

    def test_setting_a_fleets_origin_does_not_touch_another_fleets_runs(self, db_session):
        """Blast-radius guard: the repair UPDATE is scoped, not global."""
        ours = _mk_fleet(db_session)
        theirs = _mk_fleet(db_session)
        assert ours.id != theirs.id
        m_ours = _mk_member(db_session, ours)
        m_theirs = _mk_member(db_session, theirs)
        db_session.commit()
        for m in (m_ours, m_theirs):
            ingest_sync_report(
                db_session,
                m,
                {
                    "loop_runs": [
                        {"loop_slug": EXTERNAL_SLUG, "instance_key": uuid4().hex, "outcome": "success"}
                    ]
                },
            )

        set_fleet_origin(db_session, ours, synthetic=True)

        assert run_registry.fleet_state(db_session, ours.id)["runs"]["synthetic"] == 1
        assert run_registry.fleet_state(db_session, theirs.id)["runs"]["synthetic"] == 0


# ── THE GATE ────────────────────────────────────────────────────────────────


class TestASyntheticRunCanNeverBeCountedAsExternal:
    """Required by the phase brief: a test that FAILS if a synthetic run can
    be counted as external. Every route into an external count is asserted."""

    @pytest.mark.parametrize(
        ("fleet_kw", "member_kw", "slug", "why"),
        [
            ({}, {}, BEACON_SLUG, "known beacon slug"),
            ({}, {"is_synthetic": True}, EXTERNAL_SLUG, "member flag"),
            ({"is_synthetic": True}, {}, EXTERNAL_SLUG, "fleet flag"),
            ({}, {"key_is_test": True}, EXTERNAL_SLUG, "api key is_test"),
        ],
    )
    def test_synthetic_runs_never_reach_the_external_count(
        self, db_session, fleet_kw, member_kw, slug, why
    ):
        fleet = _mk_fleet(db_session, **fleet_kw)
        m = _mk_member(db_session, fleet, **member_kw)
        db_session.commit()

        ingest_sync_report(
            db_session,
            m,
            {"loop_runs": [{"loop_slug": slug, "instance_key": uuid4().hex, "outcome": "success"}]},
        )

        rows = db_session.query(LoopRun).filter(LoopRun.fleet_id == fleet.id).all()
        assert len(rows) == 1
        assert rows[0].is_synthetic is True, f"not classified synthetic via {why}"

        state = run_registry.fleet_state(db_session, fleet.id)
        assert state["runs"]["total"] == 1
        assert state["runs"]["synthetic"] == 1
        assert state["runs"]["external"] == 0, f"synthetic run leaked into external via {why}"

    def test_a_real_users_run_is_never_swallowed_into_synthetic(self, db_session):
        """The inverse failure would be just as dishonest — under-reporting
        the one number that matters."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        ingest_sync_report(
            db_session,
            m,
            {
                "loop_runs": [
                    {"loop_slug": EXTERNAL_SLUG, "instance_key": uuid4().hex, "outcome": "success"}
                ]
            },
        )
        state = run_registry.fleet_state(db_session, fleet.id)
        assert state["runs"] == {"total": 1, "synthetic": 0, "external": 1}

    def test_the_production_shape_reproduced(self, db_session):
        """1759 beacon runs + 1 real one. The total says 1760; adoption is 1.

        Every row here is INGESTED, never hand-stamped. Supplying
        ``is_synthetic=`` on the rows would make the assertion a tautology: it
        would hold even with ingest classification and the backfill completely
        broken, which is trap V1 inside the file that exists to close it. The
        beacon member is flagged (the identity marker); the real member is
        explicitly a customer's; nothing else tells ingest what to do.
        """
        fleet = _mk_fleet(db_session)
        beacon_member = _mk_member(db_session, fleet, host="beacon-host", is_synthetic=True)
        real_member = _mk_member(db_session, fleet, host="real-host", is_synthetic=False)
        db_session.commit()

        remaining = 1759
        while remaining:
            batch = min(remaining, 200)  # MAX_LOOP_RUNS — a real report is capped
            ingest_sync_report(
                db_session,
                beacon_member,
                {
                    "loop_runs": [
                        {
                            "loop_slug": BEACON_SLUG,
                            "instance_key": uuid4().hex,
                            "outcome": "success",
                        }
                        for _ in range(batch)
                    ]
                },
            )
            remaining -= batch
        ingest_sync_report(
            db_session,
            real_member,
            {
                "loop_runs": [
                    {"loop_slug": EXTERNAL_SLUG, "instance_key": uuid4().hex, "outcome": "success"}
                ]
            },
        )

        rows = db_session.query(LoopRun).filter(LoopRun.fleet_id == fleet.id).all()
        assert len(rows) == 1760
        assert sum(1 for r in rows if r.is_synthetic) == 1759, "ingest classification is the thing under test"

        state = run_registry.fleet_state(db_session, fleet.id)
        assert state["runs"] == {"total": 1760, "synthetic": 1759, "external": 1}

        by_slug = {lo["loop_slug"]: lo for lo in state["loops"]}
        assert by_slug[BEACON_SLUG]["external"] == 0
        assert by_slug[EXTERNAL_SLUG]["external"] == 1


# ── every surface reports BOTH ──────────────────────────────────────────────


class TestNoSurfaceReportsTheCombinedFigureAlone:
    """A count that appears without its split is the bug, even if the split
    exists somewhere else in the codebase."""

    def _fleet_with_one_of_each(self, db):
        fleet = _mk_fleet(db)
        m = _mk_member(db, fleet)
        db.commit()
        ingest_sync_report(
            db,
            m,
            {
                "loop_runs": [
                    {"loop_slug": BEACON_SLUG, "instance_key": uuid4().hex, "outcome": "success"},
                    {"loop_slug": EXTERNAL_SLUG, "instance_key": uuid4().hex, "outcome": "success"},
                ]
            },
        )
        return fleet, m

    def test_sync_report_ack(self, db_session):
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        recorded, _ = ingest_sync_report(
            db_session,
            m,
            {
                "loop_runs": [
                    {"loop_slug": BEACON_SLUG, "instance_key": uuid4().hex, "outcome": "success"},
                    {"loop_slug": EXTERNAL_SLUG, "instance_key": uuid4().hex, "outcome": "success"},
                ]
            },
        )
        assert recorded["loop_runs"] == 2
        assert recorded["loop_runs_synthetic"] == 1
        assert recorded["loop_runs_external"] == 1

    def test_run_registry_fleet_state(self, db_session):
        fleet, _ = self._fleet_with_one_of_each(db_session)
        state = run_registry.fleet_state(db_session, fleet.id)
        assert state["runs"] == {"total": 2, "synthetic": 1, "external": 1}
        for loop in state["loops"]:
            assert "synthetic" in loop and "external" in loop

    def test_trust_ledger_view(self, db_session):
        fleet, _ = self._fleet_with_one_of_each(db_session)
        ledger = run_registry.trust_ledger_view(db_session, fleet.id)
        assert ledger["runs"] == {"total": 2, "synthetic": 1, "external": 1}

    def test_daily_rollup_carries_the_split_past_raw_row_pruning(self, db_session):
        """Raw LoopRun rows are pruned at 30d. Without this column every
        adoption number older than a month would silently re-merge."""
        fleet, m = self._fleet_with_one_of_each(db_session)
        rollup_loop_runs(db_session, day=date.today())

        from app.models import LoopRunDailyRollup

        rows = {
            r.loop_slug: r
            for r in db_session.query(LoopRunDailyRollup)
            .filter(LoopRunDailyRollup.fleet_id == fleet.id)
            .all()
        }
        assert rows[BEACON_SLUG].runs == 1
        assert rows[BEACON_SLUG].synthetic_runs == 1
        assert rows[EXTERNAL_SLUG].runs == 1
        assert rows[EXTERNAL_SLUG].synthetic_runs == 0

    def test_fleet_dashboard_endpoint(self, db_session, monkeypatch):
        """The surface Adam actually reads. HTTP level, not service level."""
        from fastapi.testclient import TestClient

        from app.api_key_routes import _generate_key
        from tests._app_factory import build_test_app

        u = User(email=f"{uuid4().hex}@t.com", display_name="C", subscription_tier="pro")
        db_session.add(u)
        db_session.flush()
        pt, pfx, hs = _generate_key()
        k = APIKey(id=uuid4(), user_id=u.id, key_prefix=pfx, key_hash=hs, is_active=True)
        db_session.add(k)
        db_session.flush()
        fleet = Fleet(
            id=uuid4(),
            owner_user_id=u.id,
            name="dash",
            fleet_api_key_hash=hashlib.sha256(uuid4().bytes).hexdigest(),
        )
        db_session.add(fleet)
        db_session.flush()
        m = FleetMember(
            id=uuid4(),
            fleet_id=fleet.id,
            host="h",
            profile="d",
            skills_dir="~",
            api_key_id=k.id,
        )
        db_session.add(m)
        db_session.commit()

        ingest_sync_report(
            db_session,
            m,
            {
                "loop_runs": [
                    {"loop_slug": BEACON_SLUG, "instance_key": uuid4().hex, "outcome": "success"},
                    {"loop_slug": BEACON_SLUG, "instance_key": uuid4().hex, "outcome": "success"},
                    {"loop_slug": EXTERNAL_SLUG, "instance_key": uuid4().hex, "outcome": "success"},
                ]
            },
        )
        rollup_loop_runs(db_session, day=date.today())

        client = TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))
        body = client.get(f"/api/fleets/{fleet.id}/dashboard", headers={"x-api-key": pt}).json()

        assert body["total_runs"] == 3
        assert body["synthetic_runs"] == 2
        assert body["external_runs"] == 1


# ── ingest paths ────────────────────────────────────────────────────────────


class TestBothIngestPathsStampOrigin:
    """Two writers create LoopRun rows. A row inserted by either without the
    stamp would default to external and inflate adoption."""

    def test_run_registry_ingest_run(self, db_session):
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet)
        db_session.commit()
        run_registry.ingest_run(
            db_session,
            member_id=m.id,
            fleet_id=fleet.id,
            loop_slug=BEACON_SLUG,
            tick_id="t1",
            attempt=0,
            placement_epoch=1,
            outcome="pass",
        )
        row = db_session.query(LoopRun).filter(LoopRun.loop_slug == BEACON_SLUG).one()
        assert row.is_synthetic is True

    def test_sync_report_ingest(self, db_session):
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet, is_synthetic=True)
        db_session.commit()
        ingest_sync_report(
            db_session,
            m,
            {
                "loop_runs": [
                    {"loop_slug": EXTERNAL_SLUG, "instance_key": uuid4().hex, "outcome": "success"}
                ]
            },
        )
        row = db_session.query(LoopRun).filter(LoopRun.loop_slug == EXTERNAL_SLUG).one()
        assert row.is_synthetic is True


# ── the arithmetic ──────────────────────────────────────────────────────────


def test_run_counts_external_is_derived_not_supplied():
    """external is a subtraction, so total/synthetic/external can never
    disagree with each other."""
    c = RunCounts(total=1760, synthetic=1759)
    assert c.external == 1
    assert c.to_dict() == {"total": 1760, "synthetic": 1759, "external": 1}


def _load_migration():
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "alembic"
        / "versions"
        / "mesh0408_w4_synthetic_runs.py"
    )
    spec = importlib.util.spec_from_file_location("_w4_mig", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    return mig


# The shape of the five tables the migration touches, as they stood BEFORE it.
# Hand-written rather than derived from the ORM: the ORM already carries the
# post-migration columns, so building the fixture from it would make the
# ADD COLUMNs no-ops and the backfill untestable.
_PRE_MIGRATION_DDL = (
    "CREATE TABLE fleets (id VARCHAR(32) PRIMARY KEY, name VARCHAR(255))",
    "CREATE TABLE api_keys (id VARCHAR(32) PRIMARY KEY, is_test BOOLEAN NOT NULL DEFAULT 0)",
    "CREATE TABLE fleet_members (id VARCHAR(32) PRIMARY KEY, api_key_id VARCHAR(32))",
    "CREATE TABLE loop_runs (id VARCHAR(32) PRIMARY KEY, member_id VARCHAR(32), loop_slug VARCHAR(255))",
    "CREATE TABLE loop_run_daily_rollups ("
    " id VARCHAR(32) PRIMARY KEY, member_id VARCHAR(32), loop_slug VARCHAR(255),"
    " runs INTEGER NOT NULL DEFAULT 0)",
)


def test_migration_pins_the_application_definition_and_its_place_in_the_chain():
    """The backfill must not hard-code a literal that can drift away from
    SELF_ORIGINATED_LOOP_SLUGS — it imports the same frozenset this module
    tests, so the two can never disagree.

    Identity and chain position ONLY. This assertion cannot tell whether the
    migration does anything at all; ``test_migration_upgrade_backfills_*``
    below runs it.
    """
    mig = _load_migration()
    assert mig.SELF_ORIGINATED_LOOP_SLUGS is SELF_ORIGINATED_LOOP_SLUGS
    assert mig.down_revision == "mesh0408_t1c_extconn"
    assert mig.revision == "mesh0408_w4_synth_runs"


def test_migration_upgrade_backfills_historical_runs_by_both_routes():
    """RUNS ``upgrade()`` against a pre-migration schema and reads the rows.

    The previous version of this test asserted imported-object identity and
    ``down_revision`` and never called ``upgrade()`` — a missing or no-op
    UPDATE passed it (trap V1). This one holds four rows that a correct
    backfill must classify four different ways, so a dropped statement in
    either direction reddens it.
    """
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    mig = _load_migration()
    engine = sa.create_engine("sqlite://")

    test_key, real_key = uuid4().hex, uuid4().hex
    ci_member, real_member = uuid4().hex, uuid4().hex
    with engine.begin() as conn:
        for ddl in _PRE_MIGRATION_DDL:
            conn.execute(sa.text(ddl))
        conn.execute(
            sa.text("INSERT INTO api_keys (id, is_test) VALUES (:t, 1), (:r, 0)"),
            {"t": test_key, "r": real_key},
        )
        conn.execute(
            sa.text("INSERT INTO fleet_members (id, api_key_id) VALUES (:ci, :t), (:re, :r)"),
            {"ci": ci_member, "t": test_key, "re": real_member, "r": real_key},
        )
        rows = [
            # (id, member, slug, expected is_synthetic, why)
            ("beacon-on-unflagged", real_member, BEACON_SLUG, True),  # slug backstop
            ("ci-key-external-slug", ci_member, EXTERNAL_SLUG, True),  # APIKey.is_test
            ("real-external", real_member, EXTERNAL_SLUG, False),  # genuine adoption
            ("ci-key-beacon", ci_member, BEACON_SLUG, True),  # both routes agree
        ]
        for rid, mid, slug, _ in rows:
            conn.execute(
                sa.text(
                    "INSERT INTO loop_runs (id, member_id, loop_slug) VALUES (:i, :m, :s)"
                ),
                {"i": rid, "m": mid, "s": slug},
            )
            conn.execute(
                sa.text(
                    "INSERT INTO loop_run_daily_rollups (id, member_id, loop_slug, runs) "
                    "VALUES (:i, :m, :s, 7)"
                ),
                {"i": rid, "m": mid, "s": slug},
            )

    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            mig.upgrade()

    with engine.connect() as conn:
        got = dict(conn.execute(sa.text("SELECT id, is_synthetic FROM loop_runs")).all())
        rolled = dict(
            conn.execute(sa.text("SELECT id, synthetic_runs FROM loop_run_daily_rollups")).all()
        )
        # The identity columns land UNCLASSIFIED (NULL), not False — NULL is
        # what keeps the slug backstop live for pre-W4 rows.
        conn.execute(sa.text("INSERT INTO fleets (id, name) VALUES ('f', 'x')"))

    for rid, _, _, expected in rows:
        assert bool(got[rid]) is expected, f"loop_runs.{rid} backfilled wrong"
        assert (rolled[rid] == 7) is expected, f"rollup {rid} backfilled wrong"

    assert got["real-external"] == 0, "a genuine customer run was backfilled as ours"

    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT is_synthetic FROM fleets")).scalar() is None
        with Operations.context(MigrationContext.configure(conn)):
            mig.downgrade()
    insp = sa.inspect(engine)
    assert "is_synthetic" not in {c["name"] for c in insp.get_columns("loop_runs")}
    assert "synthetic_runs" not in {c["name"] for c in insp.get_columns("loop_run_daily_rollups")}


def test_naive_timestamps_do_not_break_the_split_on_the_rollup(db_session):
    """Guard for the SQLite path, where ``DateTime(timezone=True)`` round-trips
    NAIVE. The previous version of this test called
    ``classify_run_synthetic()``, which never reads a timestamp — it could not
    have failed. The timestamp-sensitive surface is the daily rollup, which
    buckets rows by comparing ``created_at`` against an offset-AWARE day
    window; a naive row that fell out of that window would silently drop the
    split and re-merge our own beacon into adoption.
    """
    fleet = _mk_fleet(db_session)
    beacon = _mk_member(db_session, fleet, host="beacon", is_synthetic=True)
    real = _mk_member(db_session, fleet, host="real", is_synthetic=False)
    db_session.commit()

    naive_noon = datetime.now(UTC).replace(hour=12, minute=0, tzinfo=None)
    for member, slug in ((beacon, BEACON_SLUG), (real, EXTERNAL_SLUG)):
        db_session.add(
            LoopRun(
                id=uuid4(),
                member_id=member.id,
                fleet_id=fleet.id,
                loop_slug=slug,
                instance_key=uuid4().hex,
                outcome="success",
                is_synthetic=classify_run_synthetic(db_session, loop_slug=slug, member=member),
                created_at=naive_noon,
            )
        )
    db_session.commit()

    assert run_registry.fleet_state(db_session, fleet.id)["runs"] == {
        "total": 2,
        "synthetic": 1,
        "external": 1,
    }

    rollup_loop_runs(db_session, day=naive_noon.date())
    rows = {
        r.loop_slug: r
        for r in db_session.query(LoopRunDailyRollup)
        .filter(LoopRunDailyRollup.fleet_id == fleet.id)
        .all()
    }
    assert set(rows) == {BEACON_SLUG, EXTERNAL_SLUG}, "a naive timestamp fell out of the day window"
    assert rows[BEACON_SLUG].synthetic_runs == 1
    assert rows[EXTERNAL_SLUG].synthetic_runs == 0
