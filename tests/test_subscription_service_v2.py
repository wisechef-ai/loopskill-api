"""Phase A — pricing rebuild tests.

Verifies (RED then GREEN):
1. TIER_PRICE_IDS no longer contains "operator" (retired tier).
2. create_checkout_session("operator") raises SubscriptionError.
3. POST /api/checkout/operator returns 400 (invalid_tier).
4. POST /api/subscriptions/downgrade switches an All-in (studio) customer to Pro (cook)
   via stripe.Subscription.modify with proration_behavior="create_prorations".
5. /api/subscriptions/downgrade rejects non-studio users (cook → 400).
6. /api/subscriptions/downgrade requires auth (401 anon).
"""
from __future__ import annotations

import uuid
from typing import Generator
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.models import Base, User


# ── DB fixtures (module-scoped engine, per-test rollback) ────────────────

@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    @event.listens_for(engine, "connect")
    def _set_pragma(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(bind=connection, autocommit=False, autoflush=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def configured_prices(monkeypatch):
    """Test price IDs in settings + subscription_service.TIER_PRICE_IDS (no operator)."""
    from app import subscription_service as ss
    monkeypatch.setattr(settings, "STRIPE_PRICE_COOK", "price_test_cook_v2")
    monkeypatch.setattr(settings, "STRIPE_PRICE_STUDIO", "price_test_studio_v2")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_BASE", "https://recipes.test/")
    monkeypatch.setattr(ss, "TIER_PRICE_IDS", {
        "cook": "price_test_cook_v2",
        "studio": "price_test_studio_v2",
    })
    yield


def _build_test_app(db: Session) -> FastAPI:
    from app.checkout_routes import router as checkout_router

    app = FastAPI()
    def _override_get_db():
        try:
            yield db
        finally:
            pass
    app.dependency_overrides[get_db] = _override_get_db
    app.include_router(checkout_router)
    return app


@pytest.fixture
def studio_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        github_id=1_001 + int(uuid.uuid4().int) % 1_000_000,
        email=f"v2-{uuid.uuid4().hex[:6]}@test.recipes.wisechef.ai",
        display_name="V2 Studio User",
        stripe_customer_id="cus_test_studio_v2",
        subscription_id="sub_test_studio_v2",
        subscription_status="active",
        subscription_tier="studio",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def cook_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        github_id=2_001 + int(uuid.uuid4().int) % 1_000_000,
        email=f"v2cook-{uuid.uuid4().hex[:6]}@test.recipes.wisechef.ai",
        display_name="V2 Cook User",
        stripe_customer_id="cus_test_cook_v2",
        subscription_id="sub_test_cook_v2",
        subscription_status="active",
        subscription_tier="cook",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _client_for(db: Session, user: User | None) -> TestClient:
    from app import auth_routes
    app = _build_test_app(db)
    app.dependency_overrides[auth_routes.get_current_user_optional] = lambda: user
    return TestClient(app)


# ── Tier rename validation ──────────────────────────────────────────────

def test_operator_tier_retired_from_price_ids():
    """Operator slug must not appear in TIER_PRICE_IDS (production source).

    Reload subscription_service so any earlier monkeypatch from another
    test module does not mask the production state.
    """
    import importlib
    from app import subscription_service as ss
    ss = importlib.reload(ss)
    assert "operator" not in ss.TIER_PRICE_IDS
    assert set(ss.TIER_PRICE_IDS) == {"cook", "studio"}


def test_operator_restored_in_settings_defaults():
    """Settings.STRIPE_PRICE_OPERATOR field is restored in v7/phase-F (operator un-retired,
    studio → operator alias). Both env vars exist during the 90-day backward-compat window
    so older portal code reading STRIPE_PRICE_STUDIO still works."""
    from app.config import Settings
    assert "STRIPE_PRICE_OPERATOR" in Settings.model_fields
    assert "STRIPE_PRICE_STUDIO" in Settings.model_fields  # deprecated alias, kept for compat


def test_operator_checkout_raises_subscription_error(configured_prices, db, studio_user):
    """create_checkout_session('operator') must raise SubscriptionError."""
    from app.subscription_service import create_checkout_session, SubscriptionError
    with pytest.raises(SubscriptionError) as excinfo:
        create_checkout_session(user=studio_user, tier="operator", db=db)
    assert "operator" in str(excinfo.value).lower() or "unknown tier" in str(excinfo.value).lower()


def test_operator_checkout_endpoint_returns_400(configured_prices, db, studio_user):
    """POST /api/checkout/operator returns 400."""
    client = _client_for(db, studio_user)
    resp = client.post("/api/checkout/operator")
    assert resp.status_code == 400
    assert "invalid_tier" in resp.json()["detail"]


# ── Downgrade endpoint ───────────────────────────────────────────────────

def test_downgrade_studio_to_cook_calls_stripe_modify(configured_prices, db, studio_user):
    """POST /api/subscriptions/downgrade switches studio→cook with proration."""
    fake_sub = {"id": studio_user.subscription_id, "items": {"data": [{"id": "si_test_001"}]}}
    fake_modified = {
        "id": studio_user.subscription_id,
        "status": "active",
        "items": {"data": [{
            "id": "si_test_001",
            "price": {"id": "price_test_cook_v2", "metadata": {"tier": "cook"}},
        }]},
    }
    client = _client_for(db, studio_user)

    with patch("stripe.Subscription.retrieve", return_value=fake_sub) as ret_mock, \
         patch("stripe.Subscription.modify", return_value=fake_modified) as mod_mock:
        resp = client.post("/api/subscriptions/downgrade")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "cook"
    assert body["subscription_id"] == studio_user.subscription_id

    ret_mock.assert_called_once()
    mod_mock.assert_called_once()
    kwargs = mod_mock.call_args.kwargs
    assert kwargs.get("proration_behavior") == "create_prorations"
    items = kwargs.get("items") or []
    assert items and items[0].get("id") == "si_test_001"
    assert items[0].get("price") == "price_test_cook_v2"


def test_downgrade_rejects_cook_user(configured_prices, db, cook_user):
    """Only studio users can downgrade — cook returns 400."""
    client = _client_for(db, cook_user)
    resp = client.post("/api/subscriptions/downgrade")
    assert resp.status_code == 400
    assert "studio" in resp.json()["detail"].lower() or "not_studio" in resp.json()["detail"]


def test_downgrade_requires_auth(configured_prices, db):
    """Anonymous downgrade returns 401."""
    client = _client_for(db, None)
    resp = client.post("/api/subscriptions/downgrade")
    assert resp.status_code == 401
