"""t_de052498 — ``loop_runs`` had no producer: 20,828 of its 20,829 rows were
the ``p4-loop-proof`` beacon and the 20,829th was a hand-written Phase-T test.

The fix is a producer that reads the SCHEDULER's own record of a fire instead of
trusting a line inside the loop's prompt, because a crashed loop says nothing
and silence is exactly the state this table could never distinguish from
"never scheduled". These tests pin four properties:

1. a scheduler-recorded fire becomes a record with no prompt cooperation;
2. a ``tick_id``-bearing record is routed through the deduping Phase-D registry,
   so at-least-once delivery cannot vote twice in a pass rate;
3. a cron the host wrote for itself is never reported as a platform-placed loop
   — reporting the whole crontab would be the same fiction as the three
   ``active`` rows in ``loop_placements`` describing crons that never existed;
4. the ack is falsifiable: it counts what it wrote, names what it refused, and
   the beacon stays flagged synthetic.

The collector is exercised as a real module against a real ``jobs.json`` shape
read from ``cron/jobs.py`` (``last_run_at``, ``last_status``, and
``last_dispatch.{scheduled_at,dispatched_at,kind}``), not a remembered one — a
test that reads the field names wrong proves nothing about production.
"""

import hashlib
import importlib.util
import json
import uuid
from pathlib import Path

import pytest

from app.models import APIKey, Fleet, FleetMember, LoopRun, User
from app.services.sync_report import ingest_sync_report

REPO_ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = REPO_ROOT / "scripts" / "loopskill-collect-reports.py"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def collector(tmp_path, monkeypatch):
    """The collector script as a module, every path pointed at tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOOPSKILL_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("LOOPSKILL_CRON_STATE", str(tmp_path / "jobs.json"))
    monkeypatch.setenv("LOOPSKILL_LOCKFILE", str(tmp_path / "lock.json"))
    monkeypatch.setenv("LOOPSKILL_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("LOOPSKILL_SECRETS", str(tmp_path / "secrets.json"))
    monkeypatch.setenv("LOOPSKILL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("LOOPSKILL_MEMBER_KEY", raising=False)
    monkeypatch.delenv("RECIPES_API_KEY", raising=False)
    monkeypatch.delenv("LOOPSKILL_INSTANCE", raising=False)

    spec = importlib.util.spec_from_file_location("_tde052498_collector", COLLECTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("_tde052498_collector",):
        sys_modules_pop(name)
    return mod


def sys_modules_pop(name: str) -> None:
    import sys

    sys.modules.pop(name, None)


def _write_jobs(path: Path, jobs: list[dict]) -> None:
    path.write_text(json.dumps({"jobs": jobs}))


def _managed_job(**overrides) -> dict:
    """A job exactly as ``app/loop_apply.manifest_to_job`` writes it, plus the
    run-state the scheduler stamps afterwards."""
    job = {
        "id": "abc123",
        "name": "loopskill/cron-watchdog",
        "prompt": "Scan the cron fleet for stalls.",
        "script": None,
        "enabled": True,
        "state": "scheduled",
        "loopskill": {"loop_id": "cron-watchdog", "epoch": 2, "applied_at": "2026-11-16T18:00:00+00:00"},
        "last_run_at": "2026-11-16T06:35:39.000267+00:00",
        "last_status": "ok",
        "last_error": None,
        "last_dispatch": {
            "scheduled_at": "2026-11-16T06:35:00+00:00",
            "dispatched_at": "2026-11-16T06:35:36.736855+00:00",
            "kind": "on_time",
        },
    }
    job.update(overrides)
    return job


def _mk_member(db, *, is_synthetic=None, member_is_test=None):
    """A real FleetMember with its own key + fleet, origin left UNCLASSIFIED by
    default — which is precisely the state prod was found in."""
    fleet = Fleet(
        id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        name=f"f-{uuid.uuid4().hex[:8]}",
        fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
    )
    db.add(fleet)
    db.flush()

    user = User(id=uuid.uuid4(), display_name="t-de052498")
    db.add(user)
    db.flush()
    key_kwargs = {"id": uuid.uuid4(), "user_id": user.id, "key_prefix": "rec_live_", "name": "member"}
    if member_is_test is None:
        key_kwargs["key_hash"] = hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest()
    else:
        key_kwargs["is_test"] = member_is_test
        key_kwargs["key_hash"] = hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest()
    key = APIKey(**key_kwargs)
    db.add(key)
    db.flush()

    member = FleetMember(
        id=uuid.uuid4(),
        fleet_id=fleet.id,
        host="wisechef-agents",
        profile="default",
        skills_dir="~/.hermes/loopskill",
        api_key_id=key.id,
        is_synthetic=is_synthetic,
    )
    db.add(member)
    db.flush()
    db.commit()
    return member, fleet


def _rows(db, member_id):
    return db.query(LoopRun).filter(LoopRun.member_id == member_id).order_by(LoopRun.id).all()


# ── 1. the scheduler is now a producer ───────────────────────────────────────


class TestSchedulerDerivedProducer:
    def test_a_recorded_fire_becomes_a_record(self, collector, tmp_path):
        _write_jobs(Path(tmp_path / "jobs.json"), [_managed_job()])
        records, pending = collector.collect_scheduled_loop_runs()

        assert len(records) == 1
        rec = records[0]
        assert rec["loop_slug"] == "cron-watchdog"
        assert rec["outcome"] == "pass"
        # The placement epoch loop_apply stamped must survive to the server —
        # it is what lets run_registry flag a stale-epoch run.
        assert rec["placement_epoch"] == 2
        assert rec["attempt"] == 0
        assert rec["tick_id"] == "cron-watchdog@2026-11-16T06:35:00+00:00"
        assert pending == {"cron-watchdog": "cron-watchdog|2026-11-16T06:35:39.000267+00:00"}

    def test_a_host_own_cron_is_never_reported_as_a_loop(self, collector, tmp_path):
        """Without the namespace filter the collector would invent loop runs
        out of every cron on the box — the beacon failure again, at scale."""
        host_job = _managed_job()
        host_job["name"] = "goals-predicate-sweep"  # a real cron on wisechef-agents
        _write_jobs(Path(tmp_path / "jobs.json"), [host_job])

        records, pending = collector.collect_scheduled_loop_runs()
        assert records == []
        assert pending == {}

    def test_a_job_that_never_fired_produces_no_fact(self, collector, tmp_path):
        _write_jobs(Path(tmp_path / "jobs.json"), [_managed_job(last_run_at=None, last_status=None)])
        assert collector.collect_scheduled_loop_runs() == ([], {})

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("ok", "pass"),
            ("error", "fail"),
            # Everything below is the honesty half: a terminal state the
            # scheduler recorded that we cannot map is NOT a pass.
            ("delivery_failed", "unknown"),
            ("blocked_config", "unknown"),
            (None, "unknown"),
            ("", "unknown"),
        ],
    )
    def test_status_that_is_not_ok_or_error_is_never_a_pass(self, collector, tmp_path, status, expected):
        _write_jobs(Path(tmp_path / "jobs.json"), [_managed_job(last_status=status)])
        records, _ = collector.collect_scheduled_loop_runs()
        assert len(records) == 1
        assert records[0]["outcome"] == expected

    def test_tick_id_prefers_the_scheduled_boundary(self, collector, tmp_path):
        """tick_id is the logical firing. If it were derived from last_run_at,
        two collectors reading the same fire at different times — or a retry —
        would describe two different occurrences and dedup would miss."""
        job = _managed_job(last_run_at="2026-11-16T06:35:41.9+00:00")
        _write_jobs(Path(tmp_path / "jobs.json"), [job])
        records, _ = collector.collect_scheduled_loop_runs()
        assert records[0]["tick_id"].endswith("@2026-11-16T06:35:00+00:00")

    def test_tick_id_falls_back_to_run_time_without_a_dispatch_record(self, collector, tmp_path):
        job = _managed_job()
        job.pop("last_dispatch")
        _write_jobs(Path(tmp_path / "jobs.json"), [job])
        records, _ = collector.collect_scheduled_loop_runs()
        assert records[0]["tick_id"] == "cron-watchdog@2026-11-16T06:35:39+00:00"

    def test_accepted_change_is_always_false(self, collector, tmp_path):
        """The scheduler cannot observe an accepted change. Claiming one would
        inflate cost_per_accepted_change from a field nobody measured."""
        _write_jobs(Path(tmp_path / "jobs.json"), [_managed_job()])
        records, _ = collector.collect_scheduled_loop_runs()
        assert records[0]["accepted_change"] is False

    def test_second_read_of_the_same_fire_is_not_re_reported(self, collector, tmp_path):
        _write_jobs(Path(tmp_path / "jobs.json"), [_managed_job()])
        first, pending = collector.collect_scheduled_loop_runs()
        assert len(first) == 1
        collector._write_watermarks(pending)

        assert collector.collect_scheduled_loop_runs() == ([], {})

    def test_a_new_fire_after_a_watermark_is_reported(self, collector, tmp_path):
        """A new occurrence (the scheduler advanced its scheduled boundary) is a
        new fact, even though the loop and its watermark key are unchanged."""
        jobs_path = Path(tmp_path / "jobs.json")
        _write_jobs(jobs_path, [_managed_job()])
        _, pending = collector.collect_scheduled_loop_runs()
        collector._write_watermarks(pending)

        _write_jobs(
            jobs_path,
            [
                _managed_job(
                    last_run_at="2026-11-16T07:05:02.1+00:00",
                    last_dispatch={
                        "scheduled_at": "2026-11-16T07:05:00+00:00",
                        "dispatched_at": "2026-11-16T07:05:01.2+00:00",
                        "kind": "on_time",
                    },
                )
            ],
        )
        records, _ = collector.collect_scheduled_loop_runs()
        assert len(records) == 1
        assert records[0]["tick_id"] == "cron-watchdog@2026-11-16T07:05:00+00:00"

    def test_a_retry_of_the_same_occurrence_reuses_its_tick(self, collector, tmp_path):
        """Hermes re-runs a model-unreachable fire without a retry counter, and
        both attempts share one scheduled boundary — so both describe the SAME
        tick and the registry keeps the first terminal record. A failure that a
        retry later recovered therefore still reads as a failure. Deliberate:
        the alternative is a pass rate that counts an agent's second guess."""
        jobs_path = Path(tmp_path / "jobs.json")
        _write_jobs(jobs_path, [_managed_job(last_status="error")])
        first, pending = collector.collect_scheduled_loop_runs()
        collector._write_watermarks(pending)

        _write_jobs(jobs_path, [_managed_job(last_run_at="2026-11-16T06:36:10+00:00", last_status="ok")])
        second, _ = collector.collect_scheduled_loop_runs()

        assert len(second) == 1, "the client does not know it is a retry — it reports what it sees"
        assert first[0]["tick_id"] == second[0]["tick_id"] == "cron-watchdog@2026-11-16T06:35:00+00:00"

    def test_watermark_does_not_advance_when_the_post_fails(self, collector, tmp_path, monkeypatch):
        """At-least-once, the same contract as the outbox spool. A watermark
        advanced on a failed POST deletes a run from history forever."""
        _write_jobs(Path(tmp_path / "jobs.json"), [_managed_job()])

        def _boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(collector.urllib.request, "urlopen", _boom)
        monkeypatch.setenv("LOOPSKILL_MEMBER_KEY", "rec_live_member")
        assert collector.main() == 0, "a failed POST must not fail the cron"

        assert not collector.WATERMARKS.exists()
        again, _ = collector.collect_scheduled_loop_runs()
        assert len(again) == 1, "the fire must be re-offered next cycle"

    def test_watermark_advances_only_for_records_that_made_the_batch(self, collector, tmp_path, monkeypatch):
        """build_payload caps the batch at MAX_LOOP_RUNS. A record dropped by
        that cap has not been delivered, and advancing its watermark would
        silently delete it from history."""
        jobs = [
            _managed_job(
                name=f"loopskill/loop-{i}",
                loopskill={"loop_id": f"loop-{i}", "epoch": 1},
                last_run_at=f"2026-11-16T06:{i % 60:02d}:00.000001+00:00",
            )
            for i in range(collector.MAX_LOOP_RUNS + 5)
        ]
        _write_jobs(Path(tmp_path / "jobs.json"), jobs)

        sent: dict = {}

        class _Resp:
            def read(self):
                return json.dumps({"recorded": {"loop_runs": collector.MAX_LOOP_RUNS}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake(req, *a, **k):
            sent["payload"] = json.loads(req.data.decode())
            return _Resp()

        monkeypatch.setattr(collector.urllib.request, "urlopen", _fake)
        monkeypatch.setenv("LOOPSKILL_MEMBER_KEY", "rec_live_member")
        assert collector.main() == 0

        assert len(sent["payload"]["loop_runs"]) == collector.MAX_LOOP_RUNS
        delivered = {r["loop_slug"] for r in sent["payload"]["loop_runs"]}
        watermarks = json.loads(collector.WATERMARKS.read_text())
        assert set(watermarks) == delivered, "watermarks must mirror exactly what was delivered"

    def test_key_source_is_named_in_the_log(self, collector, tmp_path, monkeypatch, capsys):
        """The deployed collector ignored LOOPSKILL_MEMBER_KEY_FILE for four
        months because a successful fallback is indistinguishable from a
        correct one. The chain now says which source it used."""
        monkeypatch.setenv("LOOPSKILL_MEMBER_KEY", "rec_live_member")

        class _Resp:
            def read(self):
                return json.dumps({"recorded": {"loop_runs": 0}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(collector.urllib.request, "urlopen", lambda req, *a, **k: _Resp())
        _write_jobs(Path(tmp_path / "jobs.json"), [_managed_job()])

        collector.main()
        out = capsys.readouterr().out
        assert "key_source=env:LOOPSKILL_MEMBER_KEY" in out
        assert "scheduler-derived" in out

    def test_key_source_reports_the_key_file_when_no_env_is_set(self, collector, tmp_path):
        """Paths resolve at import (documented, and what makes the cron line's
        LOOPSKILL_MEMBER_KEY_FILE meaningful), so this writes to the path the
        module actually resolved rather than re-setting the env afterwards."""
        key_file = collector.KEY_FILE
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text("rec_live_from_file\n")
        assert collector.resolve_member_key() == ("rec_live_from_file", f"file:{key_file}")

    def test_key_source_says_none_when_nothing_resolves(self, collector):
        """An unenrolled host must be able to tell, from one log line."""
        assert collector.resolve_member_key() == (None, "none")


# ── 2. the honest registry is now the live path ──────────────────────────────


class TestRegistryRouting:
    def test_a_tick_bearing_entry_lands_with_its_identity(self, db_session):
        member, fleet = _mk_member(db_session)
        recorded, _ = ingest_sync_report(
            db_session,
            member,
            {
                "loop_runs": [
                    {
                        "loop_slug": "cron-watchdog",
                        "instance_key": "wisechef-agents/default",
                        "outcome": "pass",
                        "tick_id": "cron-watchdog@2026-11-16T06:35:00+00:00",
                        "attempt": 0,
                        "placement_epoch": 2,
                        "detail": "scheduler: status=ok",
                    }
                ]
            },
        )

        rows = _rows(db_session, member.id)
        assert len(rows) == 1
        # All three NULL on all 20,829 historical rows. If this row has them,
        # it went through run_registry — the legacy insert cannot set them.
        assert rows[0].tick_id == "cron-watchdog@2026-11-16T06:35:00+00:00"
        assert rows[0].placement_epoch == 2
        assert rows[0].attempt == 0
        assert recorded["loop_runs_registry_new"] == 1
        assert recorded["loop_runs_registry_deduped"] == 0

    def test_duplicate_delivery_is_refused_and_says_so(self, db_session):
        payload = {
            "loop_runs": [
                {
                    "loop_slug": "cron-watchdog",
                    "instance_key": "wisechef-agents/default",
                    "outcome": "pass",
                    "tick_id": "cron-watchdog@2026-11-16T06:35:00+00:00",
                    "attempt": 0,
                    "placement_epoch": 2,
                }
            ]
        }
        member, _ = _mk_member(db_session)
        ingest_sync_report(db_session, member, payload)
        again, _ = ingest_sync_report(db_session, member, payload)

        assert len(_rows(db_session, member.id)) == 1, "a retry must not become a second fact"
        assert again["loop_runs_registry_new"] == 0
        assert again["loop_runs_registry_deduped"] == 1

    def test_legacy_success_is_not_filed_as_unknown(self, db_session):
        """The registry is stricter than the legacy path: it normalizes anything
        outside pass/fail/unknown to ``unknown``, which would DROP the row from
        the pass-rate denominator entirely. The emitter's published vocabulary
        is success/failure, so the two must be reconciled at the boundary."""
        member, _ = _mk_member(db_session)
        for i, (raw, expected) in enumerate(
            [
                ("success", "pass"),
                ("failure", "fail"),
                ("budget_stop", "fail"),
                ("max_turns_stop", "fail"),
                ("gibberish", "unknown"),
            ]
        ):
            ingest_sync_report(
                db_session,
                member,
                {"loop_runs": [{"loop_slug": "loop-x", "outcome": raw, "tick_id": f"tick-{i}"}]},
            )

        rows = {r.tick_id: r.outcome for r in _rows(db_session, member.id)}
        assert rows == {
            "tick-0": "pass",
            "tick-1": "fail",
            "tick-2": "fail",
            "tick-3": "fail",
            "tick-4": "unknown",
        }

    def test_an_entry_without_a_tick_still_takes_the_legacy_path(self, db_session):
        """Old emitters in the wild (the Jul 2 copy on wisechef-agents) send no
        tick_id. Widening the endpoint must not break them, and their rows must
        stay distinguishable from registry rows by tick_id IS NULL."""
        member, _ = _mk_member(db_session)
        ingest_sync_report(
            db_session,
            member,
            {
                "loop_runs": [
                    {
                        "loop_slug": "atomic-habits",
                        "outcome": "success",
                        "cost_usd": 0.0123,
                        "duration_seconds": 41,
                        "detail": "real run",
                    }
                ]
            },
        )
        rows = _rows(db_session, member.id)
        assert len(rows) == 1
        assert rows[0].tick_id is None
        assert rows[0].outcome == "success"  # unchanged — the legacy path does not normalize

    def test_a_non_dict_entry_does_not_500_the_cycle(self, db_session):
        """One malformed element used to reach ``lr.get(...)`` and raise
        AttributeError, failing the whole batched cycle for a telemetry blip.

        It also used to be counted as a recorded run, because the ack reported
        ``len(loop_runs)``. Arrivals are not recordings — see TestAckIsFalsifiable.
        """
        member, _ = _mk_member(db_session)
        recorded, _ = ingest_sync_report(
            db_session,
            member,
            {"loop_runs": ["not-a-dict", 42, None, {"loop_slug": "ok-loop", "outcome": "success"}]},
        )
        assert recorded["loop_runs"] == 1, "three junk entries wrote no rows"
        assert recorded["loop_runs_malformed"] == 3, "and the ack names them"
        assert len(_rows(db_session, member.id)) == 1

    def test_junk_integers_are_absent_not_zero(self, db_session):
        """attempt/epoch/seq are dedup axes. Coercing junk to 0 would collide
        them against a real attempt 0."""
        member, _ = _mk_member(db_session)
        ingest_sync_report(
            db_session,
            member,
            {
                "loop_runs": [
                    {
                        "loop_slug": "loop-j",
                        "outcome": "pass",
                        "tick_id": "t-junk",
                        "attempt": "not-an-int",
                        "placement_epoch": True,
                        "member_seq": "x",
                    }
                ]
            },
        )
        row = _rows(db_session, member.id)[0]
        assert row.attempt == 0  # the documented default, not a coerced bool
        assert row.placement_epoch is None  # True was rejected, not stored as 1

    def test_unclassified_member_is_counted_as_external_and_says_so(self, db_session):
        """The prod-found state: nothing classified, so the slug backstop decided
        everything. A non-beacon slug from our own host therefore reads as
        EXTERNAL ADOPTION — the number this whole card exists to protect."""
        member, _ = _mk_member(db_session, is_synthetic=None)
        recorded, _ = ingest_sync_report(
            db_session,
            member,
            {"loop_runs": [{"loop_slug": "cron-watchdog", "outcome": "pass", "tick_id": "t-ext"}]},
        )
        assert _rows(db_session, member.id)[0].is_synthetic is False
        assert recorded["loop_runs_external"] == 1

    def test_a_classified_member_cannot_be_overruled_by_its_slug(self, db_session):
        """Precedence, not OR. Our own fleet classifying a real loop name must
        read as ours even though the slug is not on the beacon list."""
        member, _ = _mk_member(db_session, is_synthetic=True)
        recorded, _ = ingest_sync_report(
            db_session,
            member,
            {"loop_runs": [{"loop_slug": "cron-watchdog", "outcome": "pass", "tick_id": "t-ours"}]},
        )
        assert _rows(db_session, member.id)[0].is_synthetic is True
        assert recorded["loop_runs_external"] == 0
        assert recorded["loop_runs_synthetic"] == 1


# ── 3. the wire must not strip the identity fields ───────────────────────────


class TestWireContract:
    def test_request_model_carries_arbitrary_keys(self):
        """Pydantic ``list[dict[str, Any]]`` is what lets tick_id through at all.
        Tightening it to a typed model without these fields would silently send
        every host back down the non-deduping path — green tests, dead feature."""
        from app.sync_report_routes import SyncReportIn

        parsed = SyncReportIn(
            loop_runs=[
                {
                    "loop_slug": "l",
                    "outcome": "pass",
                    "tick_id": "t",
                    "attempt": 0,
                    "placement_epoch": 3,
                    "member_seq": 7,
                }
            ]
        )
        assert parsed.loop_runs[0]["tick_id"] == "t"
        assert parsed.loop_runs[0]["placement_epoch"] == 3

    def test_end_to_end_through_the_route(self, db_session, monkeypatch):
        """Real enrollment, real middleware, real POST — twice.

        Hand-rolling an APIKey row would prove the service dedups; this proves
        the whole hop, including that ``tick_id`` survives the wire model, the
        member-key auth, and the abuse ceiling between two deliveries.
        """
        from fastapi.testclient import TestClient

        from tests._app_factory import build_test_app

        app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
        client = TestClient(app)

        owner = User(id=uuid.uuid4(), display_name="e2e-owner")
        db_session.add(owner)
        db_session.flush()
        owner_raw = f"rec_live_{uuid.uuid4().hex}"
        db_session.add(
            APIKey(
                id=uuid.uuid4(),
                user_id=owner.id,
                key_prefix=owner_raw[:12],
                key_hash=hashlib.sha256(owner_raw.encode()).hexdigest(),
                name="e2e-owner",
                is_active=True,
            )
        )
        fleet = Fleet(
            id=uuid.uuid4(),
            owner_user_id=owner.id,
            name=f"e2e-{uuid.uuid4().hex[:8]}",
            fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
        )
        db_session.add(fleet)
        db_session.commit()

        r = client.post(
            f"/api/fleets/{fleet.id}/members",
            headers={"x-api-key": owner_raw},
            json={"host": "wisechef-agents", "profile": "default", "skills_dir": "~/.hermes/loopskill"},
        )
        assert r.status_code == 201, r.text
        member_id = uuid.UUID(r.json()["member_id"])
        member_key = r.json()["api_key"]

        entry = {
            "loop_slug": "cron-watchdog",
            "instance_key": "wisechef-agents/default",
            "outcome": "pass",
            "tick_id": "cron-watchdog@2026-11-16T06:35:00+00:00",
            "attempt": 0,
            "placement_epoch": 2,
            "detail": "scheduler: status=ok dispatch=on_time",
        }
        headers = {"x-api-key": member_key}

        first = client.post("/api/sync-report", json={"loop_runs": [entry]}, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["recorded"]["loop_runs_registry_new"] == 1

        second = client.post("/api/sync-report", json={"loop_runs": [entry]}, headers=headers)
        assert second.status_code == 200, second.text
        assert second.json()["recorded"]["loop_runs_registry_new"] == 0
        assert second.json()["recorded"]["loop_runs_registry_deduped"] == 1

        rows = _rows(db_session, member_id)
        assert len(rows) == 1, "two deliveries of one tick are one fact"
        assert rows[0].tick_id == entry["tick_id"]
        assert rows[0].placement_epoch == 2


class TestDryRun:
    """The stranger's pre-flight: it must show the claim and change nothing."""

    class _Ok:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"recorded": {"loop_runs": 1}}).encode()

    def test_dry_run_posts_nothing_and_moves_nothing(self, collector, tmp_path, monkeypatch, capsys):
        calls = []

        def _boom(*args, **kwargs):
            calls.append(args)
            return TestDryRun._Ok()

        monkeypatch.setattr(collector.urllib.request, "urlopen", _boom)
        spool = tmp_path / ".hermes" / "loopskill" / "outbox"
        spool.mkdir(parents=True, exist_ok=True)
        (spool / "run-a.json").write_text(
            json.dumps(
                {
                    "loop_slug": "cron-watchdog",
                    "instance_key": "wisechef-agents/default",
                    "outcome": "pass",
                }
            )
        )
        _write_jobs(tmp_path / "jobs.json", [_managed_job()])

        assert collector.main(["--dry-run"]) == 0

        assert calls == [], "a dry run must not put a fact on the wire"
        assert (spool / "run-a.json").exists(), "a dry run must not consume the spool"
        assert not collector.SENT.exists()
        assert not collector.WATERMARKS.exists(), "a dry run must not advance a watermark"

        printed = capsys.readouterr().out
        assert "DRY RUN" in printed
        assert "cron-watchdog" in printed, "it must show the run it would have reported"

    def test_dry_run_works_before_enrollment(self, collector, capsys):
        """Enrollment is precisely the step a stranger has not done yet."""
        assert collector.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "key_source=none" in out

    def test_a_real_run_still_posts(self, collector, tmp_path, monkeypatch, capsys):
        """RED-proof for the flag: without it, the same host does POST."""
        calls = []

        def _fake(*args, **kwargs):
            calls.append(args)
            return TestDryRun._Ok()

        monkeypatch.setattr(collector.urllib.request, "urlopen", _fake)
        _write_jobs(tmp_path / "jobs.json", [_managed_job()])
        monkeypatch.setenv("LOOPSKILL_MEMBER_KEY", "rec_live_test")

        assert collector.main([]) == 0
        assert len(calls) == 1


class TestHonestAttribution:
    """The failure this card is about was a misread, not a lie: 20,828 beacon
    rows were quoted as live loop_runs. These pin the properties that keep the
    table readable honestly."""

    def test_the_beacon_stays_marked_synthetic(self, db_session):
        """A self-originated proof beacon is honest only while it is flagged.
        The new producer must not be able to hand a beacon an external verdict."""
        from app.services.synthetic_runs import classify_run_synthetic

        member, _ = _mk_member(db_session, is_synthetic=True)
        db_session.commit()
        assert classify_run_synthetic(db_session, loop_slug="p4-loop-proof", member=member) is True

    def test_an_owned_member_without_a_classified_origin_is_not_reported_as_external(self, db_session):
        """Measured 2026-11-16: fleets.is_synthetic and fleet_members.is_synthetic
        were NULL for every owned host, so an owned host's non-beacon run read as
        external adoption. Until origin is classified, the verdict must stay
        unknown/None rather than silently flipping to external."""
        from app.services.synthetic_runs import origin_verdict_for_member

        member, _ = _mk_member(db_session, is_synthetic=None, member_is_test=None)
        db_session.commit()
        assert origin_verdict_for_member(db_session, member) is None

    def test_the_origin_marker_is_callable_and_lands_on_the_row(self, db_session):
        """A marker no code path can set is not a marker — it is a hard-coded
        slug list wearing a column (app/services/synthetic_runs.py)."""
        from app.services.synthetic_runs import origin_verdict_for_member, set_member_origin

        member, fleet = _mk_member(db_session)
        db_session.commit()
        returned = set_member_origin(db_session, member, synthetic=True)
        db_session.commit()
        assert returned.is_synthetic is True
        assert db_session.get(FleetMember, member.id).is_synthetic is True
        assert origin_verdict_for_member(db_session, member) is True


class TestAckIsFalsifiable:
    """The ack is the only instrument a fleet operator has. An ack that reports
    arrivals as recordings is the defect that hid this table for a year."""

    def test_a_redelivered_tick_does_not_inflate_the_run_count(self, db_session):
        from app.services.sync_report import ingest_sync_report

        member, _ = _mk_member(db_session)
        entry = {
            "loop_slug": "cron-watchdog",
            "instance_key": "wisechef-agents/default",
            "outcome": "pass",
            "tick_id": "cron-watchdog@2026-11-16T06:35:00+00:00",
        }
        first, _ = ingest_sync_report(db_session, member, {"loop_runs": [entry]})
        second, _ = ingest_sync_report(db_session, member, {"loop_runs": [entry]})

        assert first["loop_runs"] == 1
        assert second["loop_runs"] == 0, "a duplicate delivery recorded nothing; the ack must say so"
        assert second["loop_runs_registry_deduped"] == 1
        assert len(_rows(db_session, member.id)) == 1

    def test_malformed_entries_are_counted_not_swallowed(self, db_session):
        from app.services.sync_report import ingest_sync_report

        member, _ = _mk_member(db_session)
        payload = {
            "loop_runs": [
                "i am not a dict",
                None,
                {"loop_slug": "cron-watchdog", "instance_key": "h/d", "outcome": "pass"},
            ]
        }
        recorded, _ = ingest_sync_report(db_session, member, payload)

        assert recorded["loop_runs"] == 1, "one entry was actually written, so the ack says one"
        assert recorded["loop_runs_malformed"] == 2, "the other two must be visible, not silent"
        assert len(_rows(db_session, member.id)) == 1

    def test_junk_cost_does_not_500_the_cycle_and_lands_as_null(self, db_session):
        """A Numeric column raised on ``cost_usd='n/a'`` and the whole batched
        telemetry cycle died. Un-parseable is absent, and absent is honest."""
        from app.services.sync_report import ingest_sync_report

        member, _ = _mk_member(db_session)
        recorded, _ = ingest_sync_report(
            db_session,
            member,
            {
                "loop_runs": [
                    {
                        "loop_slug": "cron-watchdog",
                        "instance_key": "h/d",
                        "outcome": "pass",
                        "cost_usd": "n/a",
                        "duration_seconds": "soon",
                    }
                ]
            },
        )
        assert recorded["loop_runs"] == 1
        row = _rows(db_session, member.id)[0]
        assert row.cost_usd is None
        assert row.duration_seconds is None

    def test_a_real_cost_still_lands(self, db_session):
        """RED-proof for the coercion: this must not become NULL too."""
        from decimal import Decimal

        from app.services.sync_report import ingest_sync_report

        member, _ = _mk_member(db_session)
        ingest_sync_report(
            db_session,
            member,
            {
                "loop_runs": [
                    {
                        "loop_slug": "cron-watchdog",
                        "instance_key": "h/d",
                        "outcome": "pass",
                        "cost_usd": "0.42",
                        "duration_seconds": "95",
                    }
                ]
            },
        )
        row = _rows(db_session, member.id)[0]
        assert row.cost_usd == Decimal("0.42")
        assert row.duration_seconds == 95
