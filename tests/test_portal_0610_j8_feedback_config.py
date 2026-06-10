"""portal_0610 J8 — delivery cockpit backend: feedback-repo binding HTTP routes.

The cockpit needs an HTTP surface for per-cookbook feedback routing (the MCP
recipes_configure_feedback tool was the only entry point). These thin routes
delegate to that tool for a single source of truth.

  GET   /api/cookbooks/{id}/feedback-config  → where feedback routes (no PAT)
  PATCH /api/cookbooks/{id}/feedback-config  → bind/clear routing (Pro/Pro+ only)

Client-handoff (share-token mint) + cookbook handoff already have routes/tests
elsewhere; this suite covers the J8-new feedback binding.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    return TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))


def _mk_user(db, *, tier="pro"):
    from app.models import User

    u = User(
        id=uuid.uuid4(),
        display_name="cockpit-owner",
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
            name="j8",
            is_active=True,
            is_test=True,
        )
    )
    db.flush()
    return raw


def _mk_cookbook(db, owner):
    from app.models import Cookbook

    cb = Cookbook(id=uuid.uuid4(), name="client-deck", cookbook_owner=owner.id, visibility="private")
    db.add(cb)
    db.flush()
    return cb


def test_get_feedback_config_default(middleware_client, db_session):
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)

    r = middleware_client.get(f"/api/cookbooks/{cb.id}/feedback-config", headers={"x-api-key": key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feedback_repo"] is None
    assert body["has_credential"] is False
    assert body["default_repo"] == "wisechef-ai/recipes-api"


def test_get_feedback_config_non_owner_404(middleware_client, db_session):
    owner = _mk_user(db_session)
    other = _mk_user(db_session)
    other_key = _mk_key(db_session, other)
    cb = _mk_cookbook(db_session, owner)
    r = middleware_client.get(f"/api/cookbooks/{cb.id}/feedback-config", headers={"x-api-key": other_key})
    assert r.status_code in (403, 404)


def test_clear_feedback_config(middleware_client, db_session):
    """repo=None clears routing — no PAT needed, no GitHub call. Pure path."""
    owner = _mk_user(db_session)
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)

    r = middleware_client.patch(
        f"/api/cookbooks/{cb.id}/feedback-config",
        headers={"x-api-key": key},
        json={"repo": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json().get("cleared") is True


def test_feedback_config_free_tier_blocked(middleware_client, db_session):
    """Custom feedback routing is Pro/Pro+ only — free tier rejected (422)."""
    owner = _mk_user(db_session, tier="free")
    key = _mk_key(db_session, owner)
    cb = _mk_cookbook(db_session, owner)
    r = middleware_client.patch(
        f"/api/cookbooks/{cb.id}/feedback-config",
        headers={"x-api-key": key},
        json={"repo": "acme/feedback", "mode": "pat", "pat": "ghp_x"},
    )
    assert r.status_code == 422
    assert "pro" in r.json()["detail"].lower()
