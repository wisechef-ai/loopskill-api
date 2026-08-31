"""bhint-tel0824 (t_55a1a333) — bundle-hint telemetry + observability.

Acceptance cases from the task spec:
  1. Hint fires -> bundle_hint.shown counter row incremented (slug-keyed).
  2. compute_bundle_hint raises -> bundle_hint.error row written + log
     emitted, response still 200 with bundle_hint absent.
  3. Converted pull: install.sh fetched from the same client_ip for the
     hinted slug within 7d -> bundle_hint.converted_pull row written;
     never-hinted anonymous pulls record nothing.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Skill, TelemetryEvent
from app.services.bundle_hint_telemetry import (
    CONVERSION_WINDOW_DAYS,
    EVENT_CONVERTED_PULL,
    EVENT_ERROR,
    EVENT_SHOWN,
    maybe_record_hint_conversion,
)
from tests._app_factory import build_test_app
from tests.conftest import make_skill
from tests.test_bhint0823_bundle_fast_path import (
    HINT_IP,
    _direct_install,
    _make_bundle,
    _make_version,
)

OTHER_IP = "198.51.100.99"  # TEST-NET-2 — never a real client


@pytest.fixture()
def tel_client(db_session, monkeypatch):
    """TestClient on the canonical app factory (install route properly mounted)."""
    from app.config import settings

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, headers={"x-api-key": settings.API_KEY}, raise_server_exceptions=True)


@pytest.fixture()
def anon_client(db_session, monkeypatch):
    """Anonymous client (no key) — the install.sh pull surface."""
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, raise_server_exceptions=True)


def _tel_rows(db: Session, event_type: str) -> list[TelemetryEvent]:
    return db.query(TelemetryEvent).filter(TelemetryEvent.event_type == event_type).all()


def _seed_hint_fire(db: Session, client: TestClient, *, prefix: str = "tel-a") -> dict:
    """3 direct installs from HINT_IP -> 3rd response carries the hint; returns it."""
    skills = [make_skill(db, slug=f"{prefix}-{i}") for i in range(3)]
    for s in skills:
        _make_version(db, s)
    _make_bundle(db, "loopskill-essentials", skills)

    resp = None
    for s in skills:
        resp = _direct_install(client, s.slug)
        assert resp.status_code == 200, resp.text
    assert resp.json()["bundle_hint"] is not None
    return resp.json()["bundle_hint"]


# ── Acceptance 1: hint fires -> counter incremented ────────────────────────


def test_hint_fire_writes_shown_counter_row(db_session, tel_client):
    hint = _seed_hint_fire(db_session, tel_client)
    assert hint["slug"] == "loopskill-essentials"

    rows = _tel_rows(db_session, EVENT_SHOWN)
    assert len(rows) == 1, "exactly one bundle_hint.shown row per fire"
    row = rows[0]
    assert row.skill_slug == "loopskill-essentials", "counter is slug-keyed"
    # TestClient's socket peer is the literal string "testclient" — the
    # install event (and thus the hint row) carries that as client_ip.
    assert row.client_ip == "testclient"
    payload = json.loads(row.payload or "{}")
    assert payload["slug"] == "loopskill-essentials"
    assert payload["matched"] == "3 of 3"


def test_hint_counter_increments_per_fire(db_session, tel_client):
    """A repeat install (hint repeats per zero-false-positive spec) -> 2nd row."""
    _seed_hint_fire(db_session, tel_client, prefix="tel-inc")
    # Re-install an existing member: distinct recent skill set is still 3
    # (all in the bundle), so the hint fires again — the observed 29-install
    # repeat case. No second bundle row (slug would collide).
    again = db_session.query(Skill).filter(Skill.slug == "tel-inc-0").one()
    r = _direct_install(tel_client, again.slug)
    assert r.status_code == 200, r.text
    assert r.json()["bundle_hint"] is not None
    assert len(_tel_rows(db_session, EVENT_SHOWN)) == 2


def test_no_hint_no_counter_row(db_session, tel_client):
    """Installs that don't trigger the hint write NO telemetry rows."""
    skills = [make_skill(db_session, slug=f"tel-nohint-{i}") for i in range(2)]
    for s in skills:
        _make_version(db_session, s)
    _make_bundle(db_session, "loopskill-essentials", skills)

    for s in skills:
        r = _direct_install(tel_client, s.slug)
        assert r.status_code == 200
        assert r.json()["bundle_hint"] is None

    assert _tel_rows(db_session, EVENT_SHOWN) == []
    assert _tel_rows(db_session, EVENT_ERROR) == []


def test_install_all_carries_attribution_beacon(db_session, tel_client):
    """install_all embeds ?slug= so a real hinted pull is measurable."""
    hint = _seed_hint_fire(db_session, tel_client, prefix="tel-beacon")
    assert "install.sh?slug=loopskill-essentials" in hint["install_all"]
    # …and the script itself still receives the slug via bash argv
    assert hint["install_all"].endswith("bash -s -- loopskill-essentials")


# ── Acceptance 2: exception path -> counted + logged, response 200 ─────────


def _boom(db, *, client_ip, skill_id_being_installed=None, now=None):
    raise RuntimeError("simulated hint regression")


def test_compute_failure_writes_error_row_and_stays_200(db_session, tel_client, monkeypatch):
    """Hint regression -> bundle_hint.error rows, response 200, no shown rows."""
    import app.services.bundle_hint as bh

    monkeypatch.setattr(bh, "compute_bundle_hint", _boom)

    skills = [make_skill(db_session, slug=f"tel-err-{i}") for i in range(3)]
    for s in skills:
        _make_version(db_session, s)
    _make_bundle(db_session, "loopskill-essentials", skills)
    for s in skills:
        r = _direct_install(tel_client, s.slug)
        assert r.status_code == 200, r.text
        assert r.json().get("bundle_hint") is None
        assert "X-LoopSkill-Bundle-Hint" not in r.headers

    rows = _tel_rows(db_session, EVENT_ERROR)
    assert len(rows) == 3, "one error row per failed compute"
    assert "simulated hint regression" in (rows[0].payload or "")
    assert _tel_rows(db_session, EVENT_SHOWN) == [], "nothing shown on the error path"


def test_error_path_log_emitted(db_session, tel_client, monkeypatch, caplog):
    """The exception path emits one structured log line per failure (acceptance 2)."""
    import app.services.bundle_hint as bh

    monkeypatch.setattr(bh, "compute_bundle_hint", _boom)
    skills = [make_skill(db_session, slug=f"tel-log-{i}") for i in range(3)]
    for s in skills:
        _make_version(db_session, s)
    _make_bundle(db_session, "loopskill-essentials", skills)

    with caplog.at_level(logging.ERROR, logger="app.services.bundle_hint_telemetry"):
        for s in skills:
            r = _direct_install(tel_client, s.slug)
            assert r.status_code == 200
    assert any("bundle_hint computation failed" in rec.getMessage() for rec in caplog.records)
    assert len(_tel_rows(db_session, EVENT_ERROR)) == 3


# ── Acceptance 3: conversion attribution ────────────────────────────────────


def test_hinted_pull_records_conversion(db_session, tel_client, anon_client):
    _seed_hint_fire(db_session, tel_client, prefix="tel-conv")

    r = anon_client.get("/api/bundles/install.sh?slug=loopskill-essentials")
    assert r.status_code == 200
    assert r.text.startswith("#!/usr/bin/env bash")

    rows = _tel_rows(db_session, EVENT_CONVERTED_PULL)
    assert len(rows) == 1
    assert rows[0].skill_slug == "loopskill-essentials"
    # Both clients share the TestClient socket peer "testclient" — which is
    # exactly the IP the shown row carries, so attribution joins.
    assert rows[0].client_ip == "testclient"


def test_never_hinted_pull_records_nothing(db_session, tel_client, anon_client):
    """Pull from an IP that was never hinted -> no conversion row (no inflation)."""
    # Hint rows exist for HINT_IP (a different IP than the puller's
    # TestClient socket peer "testclient") — attribution must miss.
    db_session.add(
        TelemetryEvent(
            event_type=EVENT_SHOWN,
            skill_slug="loopskill-essentials",
            payload=json.dumps({"slug": "loopskill-essentials", "matched": "3 of 53"}),
            client_ip=HINT_IP,
        )
    )
    db_session.flush()

    r = anon_client.get(
        "/api/bundles/install.sh?slug=loopskill-essentials",
        # CF header is deliberately ignored: the direct socket peer
        # ("testclient") is not in TRUSTED_PROXY_CIDRS, so the puller's IP
        # stays "testclient" — different from HINT_IP above.
        headers={"cf-connecting-ip": OTHER_IP},
    )
    assert r.status_code == 200
    assert _tel_rows(db_session, EVENT_CONVERTED_PULL) == []


def test_conversion_window_7d(db_session):
    """shown older than 7d -> pull is NOT converted (window enforced)."""
    db_session.add(
        TelemetryEvent(
            event_type=EVENT_SHOWN,
            skill_slug="loopskill-essentials",
            payload=json.dumps({"slug": "loopskill-essentials", "matched": "3 of 53"}),
            client_ip=HINT_IP,
            created_at=datetime.now(UTC) - timedelta(days=CONVERSION_WINDOW_DAYS + 1),
        )
    )
    db_session.flush()

    converted = maybe_record_hint_conversion(
        db_session, client_ip=HINT_IP, bundle_slug="loopskill-essentials"
    )
    assert converted is False
    assert _tel_rows(db_session, EVENT_CONVERTED_PULL) == []


def test_pull_without_slug_param_records_nothing(db_session, anon_client):
    """A bare install.sh fetch (old command shape, no beacon) stays uncounted."""
    r = anon_client.get("/api/bundles/install.sh")
    assert r.status_code == 200
    assert r.text.startswith("#!/usr/bin/env bash")
    assert _tel_rows(db_session, EVENT_CONVERTED_PULL) == []


def test_maybe_conversion_missing_inputs_noop(db_session):
    assert maybe_record_hint_conversion(db_session, client_ip=None, bundle_slug="x") is False
    assert maybe_record_hint_conversion(db_session, client_ip="1.2.3.4", bundle_slug=None) is False


def test_record_event_never_raises(db_session):
    """Fail-quiet contract: a broken payload / dead session cannot propagate."""
    from app.services.bundle_hint_telemetry import record_bundle_hint_event

    class DeadSession:
        def add(self, *a, **k):
            raise RuntimeError("db gone")

        def rollback(self):
            raise RuntimeError("rollback gone too")

    # Must not raise despite both add() and rollback() blowing up.
    record_bundle_hint_event(
        DeadSession(),  # type: ignore[arg-type]
        event_type=EVENT_SHOWN,
        client_ip="1.2.3.4",
        payload={"slug": "x"},
        skill_slug="x",
    )
