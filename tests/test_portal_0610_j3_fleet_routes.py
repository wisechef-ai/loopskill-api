"""portal_0610 J3 — HTTP fleet routes (thin adapter over the MCP fleet tools).

Pins the contract the portal /fleets surface depends on:
  GET    /api/fleets                      → list caller's fleets + subscriptions
  POST   /api/fleets                      → create a fleet (plaintext key once)
  POST   /api/fleets/{id}/subscribe       → subscribe a cookbook on a channel
  POST   /api/fleets/{id}/sync            → sync (dry_run previews)

Auth: a logged-in user (via key) sees only their fleets; a non-owner is
forbidden; anonymous is 401. Response shapes mirror the MCP tool contracts so
the two surfaces never drift (PM7).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_user(db, *, tier="pro"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="fleet-owner",
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        subscription_tier=tier,
        subscription_status="active",
    )
    db.add(u)
    db.flush()
    return u


def _mk_key(db, user):
    from app.models import APIKey

    raw = f"rec_{uuid.uuid4().hex}"
    db.add(
        APIKey(
            id=uuid.uuid4(),
            user_id=user.id,
            key_prefix=raw[:8],
            key_hash=hashlib.sha256(raw.encode()).hexdigest(),
            name="j3",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _mk_cookbook(db, owner):
    from app.models import Cookbook

    cb = Cookbook(id=uuid.uuid4(), name="deck", cookbook_owner=owner.id, visibility="private")
    db.add(cb)
    db.flush()
    return cb


# ── list / create ───────────────────────────────────────────────────────────


def test_anonymous_list_401(middleware_client):
    r = middleware_client.get("/api/fleets")
    assert r.status_code == 401


def test_create_then_list(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)

    r = middleware_client.post("/api/fleets", headers={"x-api-key": key}, json={"name": "alpha-fleet"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "alpha-fleet"
    assert body["fleet_key"].startswith("rec_fleet_")  # plaintext shown once
    assert "fleet_id" in body

    r2 = middleware_client.get("/api/fleets", headers={"x-api-key": key})
    assert r2.status_code == 200
    fleets = r2.json()["fleets"]
    assert len(fleets) == 1
    assert fleets[0]["name"] == "alpha-fleet"
    assert fleets[0]["subscriptions"] == []


def test_create_empty_name_422(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    r = middleware_client.post("/api/fleets", headers={"x-api-key": key}, json={"name": "   "})
    assert r.status_code == 422


# ── subscribe ─────────────────────────────────────────────────────────────


def test_subscribe_cookbook(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    create = middleware_client.post("/api/fleets", headers={"x-api-key": key}, json={"name": "f"})
    fid = create.json()["fleet_id"]

    r = middleware_client.post(
        f"/api/fleets/{fid}/subscribe",
        headers={"x-api-key": key},
        json={"cookbook_id": str(cb.id), "channel": "stable"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["channel"] == "stable"

    # Now it shows in the list
    lst = middleware_client.get("/api/fleets", headers={"x-api-key": key}).json()["fleets"]
    assert lst[0]["subscriptions"][0]["cookbook_id"] == str(cb.id)


def test_subscribe_invalid_channel_422(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    fid = middleware_client.post("/api/fleets", headers={"x-api-key": key}, json={"name": "f"}).json()[
        "fleet_id"
    ]
    r = middleware_client.post(
        f"/api/fleets/{fid}/subscribe",
        headers={"x-api-key": key},
        json={"cookbook_id": str(cb.id), "channel": "turbo"},
    )
    assert r.status_code == 422  # R8 channel validation enforced


def test_subscribe_nonexistent_fleet_404(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    r = middleware_client.post(
        f"/api/fleets/{uuid.uuid4()}/subscribe",
        headers={"x-api-key": key},
        json={"cookbook_id": str(cb.id)},
    )
    assert r.status_code == 404


# ── ownership isolation ──────────────────────────────────────────────────────


def test_non_owner_cannot_subscribe(middleware_client, db_session):
    owner = _mk_user(db_session)
    owner_key = _mk_key(db_session, owner)
    other = _mk_user(db_session)
    other_key = _mk_key(db_session, other)
    cb = _mk_cookbook(db_session, other)
    fid = middleware_client.post("/api/fleets", headers={"x-api-key": owner_key}, json={"name": "f"}).json()[
        "fleet_id"
    ]

    # other user tries to subscribe to owner's fleet
    r = middleware_client.post(
        f"/api/fleets/{fid}/subscribe",
        headers={"x-api-key": other_key},
        json={"cookbook_id": str(cb.id)},
    )
    assert r.status_code == 403


def test_list_isolates_per_owner(middleware_client, db_session):
    a = _mk_user(db_session)
    a_key = _mk_key(db_session, a)
    b = _mk_user(db_session)
    b_key = _mk_key(db_session, b)
    middleware_client.post("/api/fleets", headers={"x-api-key": a_key}, json={"name": "a-fleet"})

    # b sees zero fleets
    assert middleware_client.get("/api/fleets", headers={"x-api-key": b_key}).json()["fleets"] == []


# ── sync ────────────────────────────────────────────────────────────────────


def test_sync_dry_run(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    fid = middleware_client.post("/api/fleets", headers={"x-api-key": key}, json={"name": "f"}).json()[
        "fleet_id"
    ]
    middleware_client.post(
        f"/api/fleets/{fid}/subscribe",
        headers={"x-api-key": key},
        json={"cookbook_id": str(cb.id), "channel": "stable"},
    )
    r = middleware_client.post(f"/api/fleets/{fid}/sync", headers={"x-api-key": key}, json={"dry_run": True})
    assert r.status_code == 200, r.text
    assert r.json()["fleet_id"] == fid
    assert "cookbooks_synced" in r.json()
