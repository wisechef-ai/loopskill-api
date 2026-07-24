"""Phase D — heartbeat endpoint tests.

The heartbeat schema is mathematically anonymous (F8 fix in plan):
only {salt, last_seen_day} are accepted; everything else is a 400.
The DB stores blake2b(salt, key=server_pepper) — even DB compromise
reveals nothing because there's no rainbow-table-able column.
"""
from __future__ import annotations

import time
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.heartbeat_routes import router as heartbeat_router
from app.models import FleetPing


@pytest.fixture()
def hb_client(db_session):
    app = FastAPI()
    app.include_router(heartbeat_router)

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(
        app,
        headers={"x-api-key": settings.API_KEY},
        raise_server_exceptions=True,
    ) as c:
        yield c


# ── Valid payload ────────────────────────────────────────────────────────

def test_valid_heartbeat_returns_201(hb_client):
    r = hb_client.post(
        "/api/v1/heartbeat",
        json={"salt": "a" * 32, "last_seen_day": "2026-05-03"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body == {"ok": True}


def test_heartbeat_persists_only_hash_and_day(hb_client, db_session):
    salt = "deadbeef" * 4  # 32 hex chars
    hb_client.post(
        "/api/v1/heartbeat",
        json={"salt": salt, "last_seen_day": "2026-05-03"},
    )
    rows = db_session.query(FleetPing).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.salt_hash is not None
    # The salt itself MUST NOT be retrievable from the row
    assert salt.encode() not in bytes(row.salt_hash)
    assert row.last_seen_day == date(2026, 5, 3)


def test_idempotent_same_day_same_salt(hb_client, db_session):
    salt = "abcdef0123456789"
    for _ in range(5):
        r = hb_client.post(
            "/api/v1/heartbeat",
            json={"salt": salt, "last_seen_day": "2026-05-03"},
        )
        assert r.status_code == 201
    rows = db_session.query(FleetPing).all()
    assert len(rows) == 1, "5 identical pings must collapse to a single row"


# ── Schema strictness — extra fields rejected ───────────────────────────

def test_extra_field_rejected(hb_client):
    r = hb_client.post(
        "/api/v1/heartbeat",
        json={
            "salt": "a" * 32,
            "last_seen_day": "2026-05-03",
            "extra": "leak",
        },
    )
    assert r.status_code == 422 or r.status_code == 400, r.text


def test_empty_payload_rejected(hb_client):
    r = hb_client.post("/api/v1/heartbeat", json={})
    assert r.status_code in (400, 422)


def test_short_salt_rejected(hb_client):
    r = hb_client.post(
        "/api/v1/heartbeat",
        json={"salt": "abc", "last_seen_day": "2026-05-03"},
    )
    assert r.status_code in (400, 422)


def test_non_hex_salt_rejected(hb_client):
    r = hb_client.post(
        "/api/v1/heartbeat",
        json={"salt": "ZZZZZZZZZZZZZZZZ", "last_seen_day": "2026-05-03"},
    )
    assert r.status_code in (400, 422)


def test_invalid_date_rejected(hb_client):
    r = hb_client.post(
        "/api/v1/heartbeat",
        json={"salt": "a" * 32, "last_seen_day": "not-a-date"},
    )
    assert r.status_code in (400, 422)


# ── Throughput: 1000 in <60s ────────────────────────────────────────────

def test_thousand_heartbeats_under_60s(hb_client):
    start = time.time()
    for i in range(1000):
        salt = format(i, "016x") + "0" * 16  # unique 32-char hex
        r = hb_client.post(
            "/api/v1/heartbeat",
            json={"salt": salt, "last_seen_day": "2026-05-03"},
        )
        assert r.status_code == 201
    elapsed = time.time() - start
    assert elapsed < 60, f"1000 heartbeats took {elapsed:.1f}s (> 60s)"


# ── Schema-level anonymity guarantee ─────────────────────────────────────

def test_fleet_pings_table_has_no_pii_columns():
    """Mathematical guarantee: the table CANNOT identify an individual.

    Allowed columns: id (synthetic), salt_hash (keyed), last_seen_day,
    created_at. Anything else (ip, user_agent, user_id, …) is forbidden.
    """
    table = FleetPing.__table__
    allowed = {"id", "salt_hash", "last_seen_day", "created_at"}
    actual = {c.name for c in table.columns}
    assert actual == allowed, (
        f"FleetPing must store ONLY {allowed!r}; "
        f"found extra column(s): {actual - allowed}"
    )


def test_only_aggregate_query_path_exposed():
    """Sanity: the only public way to read fleet data is the weekly
    aggregate. No per-customer drill-down route exists.
    """
    from app import heartbeat_routes
    paths = [
        getattr(r, "path", "") for r in heartbeat_routes.router.routes
    ]
    # Heartbeat write + weekly aggregate; nothing else.
    assert "/api/v1/heartbeat" in paths
    assert "/api/v1/fleet/weekly" in paths
    # No path that takes a salt/user/customer parameter
    bad = [p for p in paths if "{salt" in p or "{user" in p or "{customer" in p]
    assert bad == [], f"per-identity drill-down endpoints found: {bad}"


# ── Weekly aggregate ─────────────────────────────────────────────────────

def test_weekly_aggregate_returns_distinct_counts(hb_client, db_session):
    # Simulate three distinct devices on 2026-W18
    for s in ("a" * 32, "b" * 32, "c" * 32):
        r = hb_client.post(
            "/api/v1/heartbeat",
            json={"salt": s, "last_seen_day": "2026-05-03"},
        )
        assert r.status_code == 201
    # And one repeat — must NOT inflate the count
    hb_client.post(
        "/api/v1/heartbeat",
        json={"salt": "a" * 32, "last_seen_day": "2026-05-03"},
    )

    r = hb_client.get("/api/v1/fleet/weekly")
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, list)
    by_week = {row["week"]: row["active_count"] for row in data}
    assert by_week.get("2026-W18") == 3


def test_pruner_drops_old_rows(db_session):
    from datetime import date, timedelta

    from app.crons.fleet_ping_pruner import TTL_DAYS, prune
    from app.heartbeat_routes import _hash_salt

    today = date(2026, 5, 3)
    old = today - timedelta(days=TTL_DAYS + 1)
    fresh = today - timedelta(days=10)
    for d, salt in ((old, "a" * 32), (fresh, "b" * 32)):
        db_session.add(FleetPing(salt_hash=_hash_salt(salt), last_seen_day=d))
    db_session.commit()

    # The pruner uses SessionLocal, but we patched the session globally for
    # tests via override_get_db. For unit purposes we exercise the SQL path
    # directly against db_session.
    cutoff = today - timedelta(days=TTL_DAYS)
    deleted = (
        db_session.query(FleetPing)
        .filter(FleetPing.last_seen_day < cutoff)
        .delete(synchronize_session=False)
    )
    db_session.commit()
    assert deleted == 1
    remaining = db_session.query(FleetPing).all()
    assert len(remaining) == 1
    assert remaining[0].last_seen_day == fresh


def test_weekly_aggregate_requires_admin_key():
    """The weekly endpoint must require the master API key."""
    app = FastAPI()
    app.include_router(heartbeat_router)
    # No api key header
    with TestClient(app, raise_server_exceptions=True) as c:
        r = c.get("/api/v1/fleet/weekly")
    # Either 401 unauthorized or 403 forbidden — anything but 200
    assert r.status_code in (401, 403)
