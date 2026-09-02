"""flywheel_0902/B — funnel ledger tests.

Covers: idempotency replay, NULL-ip=unknown classification, fleet exclusion
via config, summary conversion computed on UNIQUE stranger entities (the
council's concrete false-green case), the paid dedup invariant, and route
auth (anon 401, fleet-owner 200).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.models import Fleet, InstallEvent, Skill, User
from app.services.funnel_backfill import backfill_installed, backfill_signup
from app.services.funnel_ledger import (
    classify,
    clear_fleet_exclusions_cache,
    record_event,
    record_run,
    resolve_entity,
)


@pytest.fixture(autouse=True)
def _reset_exclusions_cache(monkeypatch):
    """Every test gets a clean fleet-exclusions cache, pointed at the real
    shipped config file unless a test overrides FUNNEL_FLEET_EXCLUSIONS_PATH.
    """
    clear_fleet_exclusions_cache()
    yield
    clear_fleet_exclusions_cache()


@pytest.fixture
def client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, raise_server_exceptions=True)


def _mk_user(db, *, email=None):
    email = email or f"{uuid.uuid4().hex[:8]}@example.com"
    u = User(id=uuid.uuid4(), display_name="funnel-test-user", email=email)
    db.add(u)
    db.flush()
    return u


def _mk_fleet(db, owner):
    fleet = Fleet(
        id=uuid.uuid4(),
        owner_user_id=owner.id,
        name="funnel-test-fleet",
        fleet_api_key_hash=hashlib.sha256(uuid.uuid4().hex.encode()).hexdigest(),
    )
    db.add(fleet)
    db.flush()
    return fleet


def _mk_skill(db):
    s = Skill(id=uuid.uuid4(), slug=f"skill-{uuid.uuid4().hex[:8]}", title="Test Skill", is_public=True)
    db.add(s)
    db.flush()
    return s


# ── 1. record_event idempotency replay ────────────────────────────────────


def test_record_event_idempotent_on_source_tuple(db_session):
    entity_id = resolve_entity(db_session, "email", "stranger1@example.com")

    row1, replay1 = record_event(
        db_session,
        stage="signup",
        entity_id=entity_id,
        source_system="loopskill-api",
        source_event_id="user-123",
        source_loop="test-loop",
        host="test-host",
        classification="stranger",
    )
    db_session.commit()
    assert replay1 is False

    row2, replay2 = record_event(
        db_session,
        stage="signup",
        entity_id=entity_id,
        source_system="loopskill-api",
        source_event_id="user-123",
        source_loop="a-different-loop",  # different loop, SAME source tuple
        host="a-different-host",
        classification="stranger",
    )
    db_session.commit()
    assert replay2 is True
    # The replay returns the ORIGINAL row untouched, not a re-dated one.
    assert row2.id == row1.id
    assert row2.source_loop == "test-loop"


def test_record_event_distinct_stage_is_not_a_replay(db_session):
    """The same source_event_id at a DIFFERENT stage is a distinct row —
    the tuple includes stage on purpose (a user.id legitimately produces
    both a 'signup' row and, via a different backfill call, could in theory
    be reused for another stage keyed differently)."""
    entity_id = resolve_entity(db_session, "email", "stranger2@example.com")

    _row1, replay1 = record_event(
        db_session,
        stage="signup",
        entity_id=entity_id,
        source_system="loopskill-api",
        source_event_id="user-456",
        source_loop="test-loop",
        host="h",
        classification="stranger",
    )
    _row2, replay2 = record_event(
        db_session,
        stage="lead",
        entity_id=entity_id,
        source_system="loopskill-api",
        source_event_id="user-456",
        source_loop="test-loop",
        host="h",
        classification="stranger",
    )
    db_session.commit()
    assert replay1 is False
    assert replay2 is False


# ── 2. NULL client_ip => unknown, never stranger ──────────────────────────


def test_null_ip_classifies_unknown_not_stranger():
    classification, evidence = classify(ip=None)
    assert classification == "unknown"
    assert "no identifier" in evidence


def test_backfill_installed_null_ip_never_stranger(db_session):
    skill = _mk_skill(db_session)
    event = InstallEvent(id=uuid.uuid4(), skill_id=skill.id, client_ip=None)
    db_session.add(event)
    db_session.flush()

    result = backfill_installed(db_session, host="test-host", dry_run=False)
    db_session.commit()

    assert result.written == 1
    from app.models import FunnelEvent

    row = (
        db_session.query(FunnelEvent)
        .filter(FunnelEvent.source_event_id == str(event.id), FunnelEvent.stage == "installed")
        .one()
    )
    assert row.classification == "unknown"
    assert row.classification != "stranger"


def test_backfill_installed_fleet_ip_classifies_fleet(db_session):
    skill = _mk_skill(db_session)
    event = InstallEvent(id=uuid.uuid4(), skill_id=skill.id, client_ip="195.128.172.227")
    db_session.add(event)
    db_session.flush()

    result = backfill_installed(db_session, host="test-host", dry_run=False)
    db_session.commit()

    assert result.written == 1
    from app.models import FunnelEvent

    row = (
        db_session.query(FunnelEvent)
        .filter(FunnelEvent.source_event_id == str(event.id), FunnelEvent.stage == "installed")
        .one()
    )
    assert row.classification == "fleet"


def test_backfill_installed_stranger_ip_classifies_stranger(db_session):
    skill = _mk_skill(db_session)
    event = InstallEvent(id=uuid.uuid4(), skill_id=skill.id, client_ip="8.8.8.8")
    db_session.add(event)
    db_session.flush()

    result = backfill_installed(db_session, host="test-host", dry_run=False)
    db_session.commit()

    assert result.written == 1
    from app.models import FunnelEvent

    row = (
        db_session.query(FunnelEvent)
        .filter(FunnelEvent.source_event_id == str(event.id), FunnelEvent.stage == "installed")
        .one()
    )
    assert row.classification == "stranger"


# ── 3. fleet exclusion via config ──────────────────────────────────────────


def test_classify_email_fleet_exclusion_from_config():
    classification, evidence = classify(email="tori@wisechef.ai")
    assert classification == "fleet"
    assert "fleet_exclusions.emails" in evidence


def test_classify_email_stranger_when_not_excluded():
    classification, _evidence = classify(email="real-customer@example.com")
    assert classification == "stranger"


def test_fleet_exclusions_config_is_overridable(tmp_path, monkeypatch):
    custom = tmp_path / "custom_exclusions.yaml"
    custom.write_text("emails:\n  - custom-fleet@example.com\nips: []\napi_key_ids: []\n")

    monkeypatch.setattr(settings, "FUNNEL_FLEET_EXCLUSIONS_PATH", str(custom))
    clear_fleet_exclusions_cache()

    classification, _ = classify(email="custom-fleet@example.com")
    assert classification == "fleet"

    # An email excluded only in the SHIPPED config is NOT excluded here.
    classification2, _ = classify(email="tori@wisechef.ai")
    assert classification2 == "stranger"

    clear_fleet_exclusions_cache()


def test_backfill_signup_fleet_email_excluded(db_session):
    fleet_user = _mk_user(db_session, email="adam.krawczyk0698@gmail.com")
    stranger_user = _mk_user(db_session, email="genuine-stranger@example.com")

    result = backfill_signup(db_session, host="test-host", dry_run=False)
    db_session.commit()

    assert result.written == 2
    from app.models import FunnelEvent

    fleet_row = (
        db_session.query(FunnelEvent)
        .filter(FunnelEvent.source_event_id == str(fleet_user.id))
        .one()
    )
    stranger_row = (
        db_session.query(FunnelEvent)
        .filter(FunnelEvent.source_event_id == str(stranger_user.id))
        .one()
    )
    assert fleet_row.classification == "fleet"
    assert stranger_row.classification == "stranger"


# ── 4. summary conversion on UNIQUE stranger entities ──────────────────────
# The council's concrete false-green case: 10 prospects logged TWICE must
# yield contacted=10 (unique entities), not 20 (raw events).


def test_summary_conversion_uses_unique_entities_not_raw_events(client, db_session):
    from app.services.funnel_ledger import resolve_entity

    for i in range(10):
        email = f"prospect{i}@example.com"
        entity_id = resolve_entity(db_session, "email", email)
        # Log the SAME entity at 'contacted' TWICE — e.g. two separate
        # outreach loops both wrote a row for the same prospect on the
        # same day via two different source_event_ids.
        record_event(
            db_session,
            stage="contacted",
            entity_id=entity_id,
            source_system="loopskill-api",
            source_event_id=f"contact-{i}-attempt-1",
            source_loop="loop-a",
            host="h",
            classification="stranger",
        )
        record_event(
            db_session,
            stage="contacted",
            entity_id=entity_id,
            source_system="loopskill-api",
            source_event_id=f"contact-{i}-attempt-2",
            source_loop="loop-b",
            host="h",
            classification="stranger",
        )
    db_session.commit()

    resp = client.get("/api/funnel/summary?since=2020-01-01T00:00:00Z")
    assert resp.status_code == 200
    body = resp.json()

    contacted = body["stages"]["contacted"]
    assert contacted["unique_stranger_entities"] == 10, (
        "council false-green case: 10 prospects logged twice must yield "
        f"contacted=10 (unique entities), got {contacted['unique_stranger_entities']}"
    )
    assert contacted["events"] == 20, "raw event count should still show all 20 writes"


def test_summary_conversion_percentage_math(client, db_session):
    from app.services.funnel_ledger import resolve_entity

    # 4 leads
    for i in range(4):
        entity_id = resolve_entity(db_session, "email", f"lead{i}@example.com")
        record_event(
            db_session,
            stage="lead",
            entity_id=entity_id,
            source_system="loopskill-api",
            source_event_id=f"lead-{i}",
            source_loop="l",
            host="h",
            classification="stranger",
        )
        # Only 2 of the 4 progress to contacted
        if i < 2:
            record_event(
                db_session,
                stage="contacted",
                entity_id=entity_id,
                source_system="loopskill-api",
                source_event_id=f"contacted-{i}",
                source_loop="l",
                host="h",
                classification="stranger",
            )
    db_session.commit()

    resp = client.get("/api/funnel/summary?since=2020-01-01T00:00:00Z")
    body = resp.json()
    assert body["conversion_pct"]["lead_to_contacted"] == 50.0


def test_summary_unknown_never_counted_as_stranger(client, db_session):
    from app.services.funnel_ledger import resolve_entity

    entity_id = resolve_entity(db_session, "ip", "unresolvable-anon-1")
    record_event(
        db_session,
        stage="installed",
        entity_id=entity_id,
        source_system="loopskill-api",
        source_event_id="install-null-ip-1",
        source_loop="l",
        host="h",
        classification="unknown",
        classification_evidence="no identifier supplied",
    )
    db_session.commit()

    resp = client.get("/api/funnel/summary?since=2020-01-01T00:00:00Z")
    body = resp.json()
    installed = body["stages"]["installed"]
    assert installed["unique_unknown_entities"] == 1
    assert installed["unique_stranger_entities"] == 0


def test_summary_no_pii_in_response(client, db_session):
    entity_id = resolve_entity(db_session, "email", "should-not-leak@example.com")
    record_event(
        db_session,
        stage="lead",
        entity_id=entity_id,
        source_system="loopskill-api",
        source_event_id="pii-check-1",
        source_loop="l",
        host="h",
        classification="stranger",
    )
    db_session.commit()

    resp = client.get("/api/funnel/summary?since=2020-01-01T00:00:00Z")
    body_text = resp.text
    assert "should-not-leak@example.com" not in body_text
    assert str(entity_id) not in body_text


# ── 5. paid invariant: ledger paid count == distinct stripe ids fed in ────


def test_paid_invariant_dedup_invoice_backed_payment_intent(db_session):
    from app.services.funnel_backfill import backfill_paid

    invoices = [
        {"id": "in_1", "amount_paid": 4900, "currency": "usd", "customer": "cus_A"},
        {"id": "in_2", "amount_paid": 995, "currency": "usd", "customer": "cus_B"},
    ]
    payment_intents = [
        # This PI is invoice-backed — MUST be dropped (already covered by in_1).
        {"id": "pi_1", "amount": 4900, "status": "succeeded", "invoice": "in_1", "customer": "cus_A"},
        # This PI is NOT invoice-backed — a genuine one-time charge, counted.
        {"id": "pi_2", "amount": 4900, "status": "succeeded", "invoice": None, "customer": "cus_C"},
        # A failed PI must never be counted.
        {"id": "pi_3", "amount": 995, "status": "requires_payment_method", "invoice": None, "customer": "cus_D"},
    ]

    result = backfill_paid(
        db_session, host="test-host", invoices=invoices, payment_intents=payment_intents, dry_run=False
    )
    db_session.commit()

    distinct_stripe_ids_fed = {"in_1", "in_2", "pi_2"}  # pi_1 dedup'd, pi_3 not succeeded
    assert result.written == len(distinct_stripe_ids_fed)

    from app.models import FunnelEvent

    ledger_source_ids = {
        row.source_event_id
        for row in db_session.query(FunnelEvent).filter(FunnelEvent.stage == "paid").all()
    }
    assert ledger_source_ids == distinct_stripe_ids_fed


def test_paid_invariant_idempotent_on_rerun(db_session):
    from app.services.funnel_backfill import backfill_paid

    invoices = [{"id": "in_rerun_1", "amount_paid": 4900, "currency": "usd", "customer": "cus_X"}]

    result1 = backfill_paid(db_session, host="h", invoices=invoices, payment_intents=[], dry_run=False)
    db_session.commit()
    assert result1.written == 1
    assert result1.replayed == 0

    result2 = backfill_paid(db_session, host="h", invoices=invoices, payment_intents=[], dry_run=False)
    db_session.commit()
    assert result2.written == 0
    assert result2.replayed == 1


# ── 6. loop_runs_ledger — separate from funnel_events, NOT deduped ────────


def test_record_run_is_not_deduped(db_session):
    """Unlike record_event, record_run writes every call — a loop that runs
    3x/day should show 3 rows, since loop_runs answers 'did it run', a
    legitimately-repeatable fact (council v2 §0.9 Finding #1)."""
    for _ in range(3):
        record_run(db_session, job_id="loop-a", loop_name="Loop A", host="h", outcome="ok", rows_emitted=1)
    db_session.commit()

    from app.models import LoopRunLedger

    count = db_session.query(LoopRunLedger).filter(LoopRunLedger.job_id == "loop-a").count()
    assert count == 3


def test_summary_runs_last_24h_reflects_run_count_not_funnel_events(client, db_session):
    for _ in range(4):
        record_run(db_session, job_id="loop-b", loop_name="Loop B", host="h", outcome="ok")
    db_session.commit()

    resp = client.get("/api/funnel/summary?since=2020-01-01T00:00:00Z")
    body = resp.json()
    assert body["runs_last_24h"].get("Loop B") == 4


# ── 7. route auth: anon 401, fleet-owner 200 ───────────────────────────────


def test_post_funnel_events_anonymous_401(client):
    resp = client.post(
        "/api/funnel/events",
        json={
            "stage": "lead",
            "source_system": "test",
            "source_event_id": "x-1",
            "source_loop": "test-loop",
            "host": "h",
            "identifier_kind": "email",
            "identifier_value": "anon@example.com",
        },
    )
    assert resp.status_code == 401


def test_post_funnel_events_master_key_201(client):
    resp = client.post(
        "/api/funnel/events",
        headers={"x-api-key": settings.API_KEY},
        json={
            "stage": "lead",
            "source_system": "test",
            "source_event_id": "x-2",
            "source_loop": "test-loop",
            "host": "h",
            "identifier_kind": "email",
            "identifier_value": "master-write@example.com",
            "classification": "stranger",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["replay"] is False


def test_post_funnel_events_fleet_owner_201(client, db_session):
    import hashlib as _hashlib

    owner = _mk_user(db_session)
    _mk_fleet(db_session, owner)

    from app.models import APIKey

    raw_key = f"rec_live_{uuid.uuid4().hex}"
    db_session.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=owner.id,
            key_prefix=raw_key[:12],
            key_hash=_hashlib.sha256(raw_key.encode()).hexdigest(),
            name="owner-key",
            is_active=True,
            is_test=True,
        )
    )
    db_session.commit()

    resp = client.post(
        "/api/funnel/events",
        headers={"x-api-key": raw_key},
        json={
            "stage": "lead",
            "source_system": "test",
            "source_event_id": "x-3",
            "source_loop": "test-loop",
            "host": "h",
            "identifier_kind": "email",
            "identifier_value": "fleet-owner-write@example.com",
            "classification": "stranger",
        },
    )
    assert resp.status_code == 201, resp.text


def test_post_funnel_events_non_owner_user_403(client, db_session):
    """A logged-in user who owns NO fleet gets 403, not 201."""
    import hashlib as _hashlib

    non_owner = _mk_user(db_session)

    from app.models import APIKey

    raw_key = f"rec_live_{uuid.uuid4().hex}"
    db_session.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=non_owner.id,
            key_prefix=raw_key[:12],
            key_hash=_hashlib.sha256(raw_key.encode()).hexdigest(),
            name="non-owner-key",
            is_active=True,
            is_test=True,
        )
    )
    db_session.commit()

    resp = client.post(
        "/api/funnel/events",
        headers={"x-api-key": raw_key},
        json={
            "stage": "lead",
            "source_system": "test",
            "source_event_id": "x-4",
            "source_loop": "test-loop",
            "host": "h",
            "identifier_kind": "email",
            "identifier_value": "non-owner@example.com",
        },
    )
    assert resp.status_code == 403


def test_post_funnel_runs_anonymous_401(client):
    resp = client.post(
        "/api/funnel/runs",
        json={"job_id": "j1", "loop_name": "Loop", "host": "h", "outcome": "ok"},
    )
    assert resp.status_code == 401


def test_post_funnel_runs_master_key_201(client):
    resp = client.post(
        "/api/funnel/runs",
        headers={"x-api-key": settings.API_KEY},
        json={"job_id": "j2", "loop_name": "Loop", "host": "h", "outcome": "ok", "rows_emitted": 3},
    )
    assert resp.status_code == 201, resp.text


def test_get_funnel_summary_is_public_no_auth_needed(client):
    resp = client.get("/api/funnel/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["stages"].keys()) == {
        "lead",
        "contacted",
        "replied",
        "signup",
        "installed",
        "bundle_created",
        "paid",
    }
