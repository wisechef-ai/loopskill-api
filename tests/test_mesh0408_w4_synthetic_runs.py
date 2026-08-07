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
from uuid import uuid4

import pytest

from app.models import APIKey, Fleet, FleetMember, LoopRun, User
from app.services import run_registry
from app.services.sync_report import ingest_sync_report, rollup_loop_runs
from app.services.synthetic_runs import (
    SELF_ORIGINATED_LOOP_SLUGS,
    RunCounts,
    classify_run_synthetic,
    member_is_synthetic,
)

BEACON_SLUG = "p4-loop-proof"
EXTERNAL_SLUG = "atomic-habits"


def _mk_fleet(db, *, is_synthetic=False):
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


def _mk_member(db, fleet, *, host="h", is_synthetic=False, key_is_test=False):
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
        m = _mk_member(db_session, fleet)  # member itself NOT flagged
        db_session.commit()
        assert m.is_synthetic is False
        assert member_is_synthetic(db_session, m) is True

    def test_api_key_is_test_marks_the_member(self, db_session):
        """Follows the spotify_0608/B precedent (APIKey.is_test) rather than
        inventing a fourth vocabulary for the same idea."""
        fleet = _mk_fleet(db_session)
        m = _mk_member(db_session, fleet, key_is_test=True)
        db_session.commit()
        assert m.is_synthetic is False
        assert fleet.is_synthetic is False
        assert member_is_synthetic(db_session, m) is True

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
        """1759 beacon runs + 1 real one. The total says 1760; adoption is 1."""
        fleet = _mk_fleet(db_session)
        beacon_member = _mk_member(db_session, fleet, host="beacon-host", is_synthetic=True)
        real_member = _mk_member(db_session, fleet, host="real-host")
        db_session.commit()

        for _ in range(1759):
            db_session.add(
                LoopRun(
                    id=uuid4(),
                    member_id=beacon_member.id,
                    fleet_id=fleet.id,
                    loop_slug=BEACON_SLUG,
                    instance_key=uuid4().hex,
                    outcome="success",
                    is_synthetic=True,
                )
            )
        db_session.add(
            LoopRun(
                id=uuid4(),
                member_id=real_member.id,
                fleet_id=fleet.id,
                loop_slug=EXTERNAL_SLUG,
                instance_key=uuid4().hex,
                outcome="success",
                is_synthetic=False,
            )
        )
        db_session.commit()

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


def test_migration_backfill_seeds_from_the_application_definition():
    """The backfill must not hard-code a literal that can drift away from
    SELF_ORIGINATED_LOOP_SLUGS — it imports the same frozenset this module
    tests, so the two can never disagree."""
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

    assert mig.SELF_ORIGINATED_LOOP_SLUGS is SELF_ORIGINATED_LOOP_SLUGS
    assert mig.down_revision == "mesh0408_t1c_extconn"


def test_naive_timestamps_do_not_break_classification(db_session):
    """Guard for the SQLite path, where DateTime(timezone=True) round-trips
    naive — classification must not depend on tzinfo at all."""
    fleet = _mk_fleet(db_session)
    m = _mk_member(db_session, fleet)
    db_session.commit()
    db_session.add(
        LoopRun(
            id=uuid4(),
            member_id=m.id,
            fleet_id=fleet.id,
            loop_slug=BEACON_SLUG,
            instance_key=uuid4().hex,
            outcome="success",
            is_synthetic=classify_run_synthetic(db_session, loop_slug=BEACON_SLUG, member=m),
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db_session.commit()
    assert run_registry.fleet_state(db_session, fleet.id)["runs"]["external"] == 0
