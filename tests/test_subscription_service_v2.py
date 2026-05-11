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
    """Test price IDs in settings + subscription_service.TIER_PRICE_IDS (operator canonical)."""
    from app import subscription_service as ss
    monkeypatch.setattr(settings, "STRIPE_PRICE_COOK", "price_test_cook_v2")
    monkeypatch.setattr(settings, "STRIPE_PRICE_STUDIO", "price_test_studio_v2")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "***")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_BASE", "https://recipes.test/")
    monkeypatch.setattr(ss, "TIER_PRICE_IDS", {
        "cook": "price_test_cook_v2",
        "operator": "price_test_studio_v2",  # operator is canonical slug (Phase 3)
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
    """User with canonical 'operator' tier (was 'studio' before Phase 3 migration)."""
    user = User(
        id=uuid.uuid4(),
        github_id=1_001 + int(uuid.uuid4().int) % 1_000_000,
        email=f"v2-{uuid.uuid4().hex[:6]}@test.recipes.wisechef.ai",
        display_name="V2 Operator User",
        stripe_customer_id="cus_test_studio_v2",
        subscription_id="sub_test_studio_v2",
        subscription_status="active",
        subscription_tier="operator",  # Phase 3: canonical slug
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

def test_operator_tier_in_price_ids():
    """operator slug must appear in TIER_PRICE_IDS (canonical slug post-Phase 3).

    Reload subscription_service so any earlier monkeypatch from another
    test module does not mask the production state.
    """
    import importlib
    from app import subscription_service as ss
    ss = importlib.reload(ss)
    assert "operator" in ss.TIER_PRICE_IDS
    assert "studio" not in ss.TIER_PRICE_IDS
    assert set(ss.TIER_PRICE_IDS) == {"cook", "operator"}


def test_legacy_studio_shim_still_accepted_in_checkout(configured_prices, db, studio_user):
    """Backwards-compat: create_checkout_session('studio') still works via shim.

    # RCP-INCIDENT-2026-05-11 backwards-compat shim test, remove after 2026-06-10
    """
    from app.subscription_service import create_checkout_session
    import stripe

    fake_customer = {"id": "cus_shim_legacy"}
    fake_session = {"id": "cs_shim_test", "url": "https://checkout.stripe.com/cs_shim"}

    studio_user.stripe_customer_id = "cus_shim_legacy"
    db.commit()

    with patch("stripe.Customer.create", return_value=fake_customer), \
         patch("stripe.checkout.Session.create", return_value=fake_session), \
         patch("stripe.PromotionCode.list", return_value={"data": []}):
        # 'studio' should be normalised to 'operator' and create_checkout_session
        # should use the 'operator' price (since TIER_PRICE_IDS has 'operator' now)
        result = create_checkout_session(user=studio_user, tier="studio", db=db)
    assert result["tier"] == "operator"  # normalised


def test_operator_restored_in_settings_defaults():
    """Settings.STRIPE_PRICE_OPERATOR field exists (operator un-retired, studio→operator)."""
    from app.config import Settings
    assert "STRIPE_PRICE_OPERATOR" in Settings.model_fields
    assert "STRIPE_PRICE_STUDIO" in Settings.model_fields  # deprecated alias, kept for compat


def test_operator_checkout_works(configured_prices, db, studio_user):
    """create_checkout_session('operator') must succeed (canonical slug post-Phase 3)."""
    from app.subscription_service import create_checkout_session

    fake_customer = {"id": "cus_test_studio_v2"}
    fake_session = {"id": "cs_op_test", "url": "https://checkout.stripe.com/cs_op_test"}

    with patch("stripe.Customer.create", return_value=fake_customer), \
         patch("stripe.checkout.Session.create", return_value=fake_session), \
         patch("stripe.PromotionCode.list", return_value={"data": []}):
        result = create_checkout_session(user=studio_user, tier="operator", db=db)
    assert result["tier"] == "operator"
    assert result["session_id"] == "cs_op_test"


def test_operator_checkout_endpoint_works(configured_prices, db, studio_user):
    """POST /api/checkout/operator returns 200 (canonical slug post-Phase 3)."""
    client = _client_for(db, studio_user)
    fake_session = {"id": "cs_ep_test", "url": "https://checkout.stripe.com/cs_ep"}
    fake_customer = {"id": "cus_ep_test"}
    with patch("stripe.Customer.create", return_value=fake_customer), \
         patch("stripe.checkout.Session.create", return_value=fake_session), \
         patch("stripe.PromotionCode.list", return_value={"data": []}):
        resp = client.post("/api/checkout/operator")
    assert resp.status_code == 200, resp.text


# ── Downgrade endpoint ───────────────────────────────────────────────────

def test_downgrade_studio_to_cook_calls_stripe_modify(configured_prices, db, studio_user):
    """POST /api/subscriptions/downgrade switches operator→cook with proration."""
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
    """Only operator users can downgrade — cook returns 400."""
    client = _client_for(db, cook_user)
    resp = client.post("/api/subscriptions/downgrade")
    assert resp.status_code == 400
    assert "operator" in resp.json()["detail"].lower() or "not_operator" in resp.json()["detail"]


def test_downgrade_requires_auth(configured_prices, db):
    """Anonymous downgrade returns 401."""
    client = _client_for(db, None)
    resp = client.post("/api/subscriptions/downgrade")
    assert resp.status_code == 401
