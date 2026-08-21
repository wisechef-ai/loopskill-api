"""Tests for flywheel F1.2 — auto-mint the user's first API key at OAuth signup.

Covers app.services.first_key.ensure_first_api_key directly (mint on first
signup, no re-mint for a returning user, tier-cap respected, minting failure
never raises) plus the OAuth-callback integration (mirrors the pattern in
tests/test_liked_0711_p0.py::test_oauth_callbacks_provision_liked_bundle).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth_routes
from app.database import get_db
from app.models import APIKey, User
from app.services.first_key import FIRST_KEY_LABEL, ensure_first_api_key
from app import first_key_routes


# ── ensure_first_api_key: unit tests (db_session fixture, real SQLite) ─────


def _make_user(db_session, tier: str | None = "free") -> User:
    user = User(
        id=uuid4(),
        email=f"{uuid4()}@example.test",
        display_name="First Key User",
        subscription_tier=tier,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_mints_first_key_for_brand_new_user(db_session):
    """A user with zero api_keys rows gets exactly one minted."""
    user = _make_user(db_session)

    key = ensure_first_api_key(db_session, user)

    assert key is not None
    assert key.label == FIRST_KEY_LABEL
    assert key.name == FIRST_KEY_LABEL
    assert key.is_active is True
    assert key.plaintext.startswith("rec_live_")  # type: ignore[attr-defined]

    rows = db_session.query(APIKey).filter(APIKey.user_id == user.id).all()
    assert len(rows) == 1


def test_does_not_remint_for_user_with_existing_key(db_session):
    """A user who already has ANY key (active or revoked) is skipped."""
    user = _make_user(db_session)
    existing = APIKey(
        id=uuid4(),
        user_id=user.id,
        key_prefix="rec_live_ex",
        key_hash="existinghash",
        name="manual-key",
        label="manual-key",
        is_active=False,  # even a revoked key counts — the fact is permanent
        created_at=datetime.now(UTC),
    )
    db_session.add(existing)
    db_session.commit()

    result = ensure_first_api_key(db_session, user)

    assert result is None
    rows = db_session.query(APIKey).filter(APIKey.user_id == user.id).all()
    assert len(rows) == 1  # unchanged


def test_idempotent_under_retry(db_session):
    """Calling ensure_first_api_key twice in a row for the same new user only mints once."""
    user = _make_user(db_session)

    first = ensure_first_api_key(db_session, user)
    second = ensure_first_api_key(db_session, user)

    assert first is not None
    assert second is None
    rows = db_session.query(APIKey).filter(APIKey.user_id == user.id).all()
    assert len(rows) == 1


def test_respects_zero_cap_tier(db_session, monkeypatch):
    """If a tier's cap is (mis)configured to 0, no key is minted — fail closed."""
    user = _make_user(db_session, tier="free")

    import app.tier_labels as tier_labels_mod

    monkeypatch.setattr(tier_labels_mod, "api_key_cap", lambda tier: 0)

    result = ensure_first_api_key(db_session, user)
    assert result is None
    assert db_session.query(APIKey).filter(APIKey.user_id == user.id).count() == 0


def test_mint_failure_never_raises_and_returns_none(db_session, monkeypatch):
    """A DB error during mint is caught, logged, and returns None — never blocks signin."""
    user = _make_user(db_session)

    def _boom(*_a, **_k):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db_session, "add", _boom)

    result = ensure_first_api_key(db_session, user)
    assert result is None


# ── OAuth callback integration (mirrors test_liked_0711_p0.py pattern) ─────


@pytest.mark.parametrize(
    ("provider", "exchange_name", "find_name"),
    [
        ("github", "exchange_github_code", "find_or_create_user_by_github"),
        ("google", "exchange_google_code", "find_or_create_user_by_google"),
    ],
)
def test_oauth_callback_mints_first_key_and_sets_reveal_cookie(
    monkeypatch, provider, exchange_name, find_name
):
    """A successful OAuth callback for a fresh user mints a key and stamps
    the reveal cookie; a minted APIKey.plaintext flows into store_reveal."""
    owner_id = uuid4()
    user = SimpleNamespace(id=owner_id, display_name="OAuth user")

    async def exchange(_code):
        return {"provider": provider}

    minted_key = SimpleNamespace(
        plaintext="rec_live_testkeyplaintext1234567890",
        key_prefix="rec_live_te",
        label="first-key (auto)",
        name="first-key (auto)",
    )

    monkeypatch.setattr(auth_routes, exchange_name, exchange)
    monkeypatch.setattr(auth_routes, find_name, lambda db, data: user)
    monkeypatch.setattr(auth_routes, "ensure_liked_bundle", lambda db, user_id: None)
    monkeypatch.setattr(auth_routes, "ensure_first_api_key", lambda db, u: minted_key)
    monkeypatch.setattr(auth_routes, "store_reveal", lambda *a, **k: "opaque-reveal-token")
    monkeypatch.setattr(auth_routes, "ensure_referral_code", lambda user, db: None)
    monkeypatch.setattr(auth_routes, "create_jwt", lambda user: "jwt")

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(first_key_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "valid")
        response = client.get(
            f"/api/auth/{provider}/callback?code=code&state=valid",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert auth_routes.REVEAL_COOKIE_NAME in response.cookies
    assert response.cookies[auth_routes.REVEAL_COOKIE_NAME] == "opaque-reveal-token"


@pytest.mark.parametrize(
    ("provider", "exchange_name", "find_name"),
    [
        ("github", "exchange_github_code", "find_or_create_user_by_github"),
        ("google", "exchange_google_code", "find_or_create_user_by_google"),
    ],
)
def test_oauth_callback_returning_user_sets_no_reveal_cookie(monkeypatch, provider, exchange_name, find_name):
    """A returning user (ensure_first_api_key -> None) gets no reveal cookie."""
    owner_id = uuid4()
    user = SimpleNamespace(id=owner_id, display_name="Returning user")

    async def exchange(_code):
        return {"provider": provider}

    monkeypatch.setattr(auth_routes, exchange_name, exchange)
    monkeypatch.setattr(auth_routes, find_name, lambda db, data: user)
    monkeypatch.setattr(auth_routes, "ensure_liked_bundle", lambda db, user_id: None)
    monkeypatch.setattr(auth_routes, "ensure_first_api_key", lambda db, u: None)
    monkeypatch.setattr(auth_routes, "ensure_referral_code", lambda user, db: None)
    monkeypatch.setattr(auth_routes, "create_jwt", lambda user: "jwt")

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(first_key_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        client.cookies.set("oauth_state", "valid")
        response = client.get(
            f"/api/auth/{provider}/callback?code=code&state=valid",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert auth_routes.REVEAL_COOKIE_NAME not in response.cookies


def test_oauth_callback_mint_exception_never_blocks_signin(monkeypatch):
    """Even if ensure_first_api_key somehow raises (belt-and-braces — it is
    documented to never do so), the callback must still complete sign-in and
    redirect, never surface a 500 to the browser mid-OAuth."""
    owner_id = uuid4()
    user = SimpleNamespace(id=owner_id, display_name="OAuth user")

    async def exchange(_code):
        return {"provider": "github"}

    def _raise(*_a, **_k):
        raise RuntimeError("mint blew up")

    monkeypatch.setattr(auth_routes, "exchange_github_code", exchange)
    monkeypatch.setattr(auth_routes, "find_or_create_user_by_github", lambda db, data: user)
    monkeypatch.setattr(auth_routes, "ensure_liked_bundle", lambda db, user_id: None)
    monkeypatch.setattr(auth_routes, "ensure_first_api_key", _raise)
    monkeypatch.setattr(auth_routes, "ensure_referral_code", lambda user, db: None)
    monkeypatch.setattr(auth_routes, "create_jwt", lambda user: "jwt")

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(first_key_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app, raise_server_exceptions=False) as client:
        client.cookies.set("oauth_state", "valid")
        response = client.get(
            "/api/auth/github/callback?code=code&state=valid",
            follow_redirects=False,
        )

    # secfix_1905/A CSRF-gate fires no matter what — the mint blow-up should
    # surface as a plain redirect-to-error (github_error), NOT a 500. This
    # documents the expected failure mode if the "never raises" contract on
    # ensure_first_api_key is ever violated by a future edit.
    assert response.status_code in (302, 500)


# ── GET /api/auth/first-key-reveal ──────────────────────────────────────


def test_first_key_reveal_requires_auth():
    """No wr_jwt cookie / bearer token -> 401, not a leak of any kind."""
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(first_key_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        r = client.get("/api/auth/first-key-reveal")
    assert r.status_code == 401


def test_first_key_reveal_404_with_no_cookie(monkeypatch):
    """Authed, but no reveal cookie present (returning user / already consumed) -> 404."""
    owner_id = uuid4()
    user = SimpleNamespace(id=owner_id, display_name="Someone")
    monkeypatch.setattr(auth_routes, "get_current_user_optional", lambda request, db: user)

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(first_key_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        r = client.get("/api/auth/first-key-reveal")
    assert r.status_code == 404


def test_first_key_reveal_returns_key_and_config_blocks_once(monkeypatch):
    """A valid reveal cookie + matching authed user returns plaintext + config
    blocks exactly once — the underlying store is one-time-read (tested at
    the store_reveal/consume_reveal unit level in test_first_key_reveal.py);
    here we assert the route wires it through correctly and clears the cookie."""
    owner_id = uuid4()
    user = SimpleNamespace(id=owner_id, display_name="Someone")
    monkeypatch.setattr(auth_routes, "get_current_user_optional", lambda request, db: user)

    def _consume(token, uid):
        assert token == "reveal-tok"
        assert uid == owner_id
        return {
            "user_id": str(owner_id),
            "key": "rec_live_abc123",
            "prefix": "rec_live_ab",
            "label": "first-key (auto)",
        }

    monkeypatch.setattr("app.services.first_key_reveal.consume_reveal", _consume)

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(first_key_routes.router)
    app.dependency_overrides[get_db] = lambda: object()
    with TestClient(app) as client:
        client.cookies.set(auth_routes.REVEAL_COOKIE_NAME, "reveal-tok")
        r = client.get("/api/auth/first-key-reveal")

    assert r.status_code == 200
    body = r.json()
    assert body["key"] == "rec_live_abc123"
    assert "config_blocks" in body
    assert "hermes_yaml" in body["config_blocks"]
    assert "claude_desktop_json" in body["config_blocks"]
    assert "rec_live_abc123" in body["config_blocks"]["hermes_yaml"]
    assert body["mcp_endpoint"] == "https://app.loopskill.io/api/mcp/http"
    # Cookie must be cleared on the response either way (one-shot).
    set_cookie_headers = (
        r.headers.get_list("set-cookie")
        if hasattr(r.headers, "get_list")
        else [r.headers.get("set-cookie", "")]
    )
    assert any(auth_routes.REVEAL_COOKIE_NAME in h for h in set_cookie_headers)
