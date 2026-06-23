"""Phase 5 — tier slug parity tests (cook→pro, operator→pro_plus).

Verifies:
1. TIER_PRICE_IDS contains 'pro' and 'pro_plus' (not legacy slugs).
2. create_checkout_session('cook') still works via URL alias shim.
3. POST /api/checkout/pro returns 200 (canonical slug).
4. POST /api/checkout/operator returns 200 (legacy alias rewritten to pro_plus).
5. POST /api/subscriptions/downgrade switches a pro_plus user to pro.
6. /api/subscriptions/downgrade rejects non-pro_plus users (pro → 400).
7. /api/subscriptions/downgrade requires auth (401 anon).
8. downgrade_pro_plus_to_pro error says 'not_pro_plus' not 'not_operator'.
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
    """Test price IDs in settings + subscription_service.TIER_PRICE_IDS (pro/pro_plus canonical)."""
    from app import subscription_service as ss
    monkeypatch.setattr(settings, "STRIPE_PRICE_COOK", "price_test_pro_v5")
    monkeypatch.setattr(settings, "STRIPE_PRICE_STUDIO", "price_test_proplus_v5")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "***")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
    monkeypatch.setattr(settings, "OAUTH_REDIRECT_BASE", "https://recipes.test/")
    monkeypatch.setattr(ss, "TIER_PRICE_IDS", {
        "pro": "price_test_pro_v5",
        "pro_plus": "price_test_proplus_v5",
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
def pro_plus_user(db) -> User:
    """User with canonical 'pro_plus' tier (Phase 5: was 'operator' before)."""
    user = User(
        id=uuid.uuid4(),
        github_id=1_001 + int(uuid.uuid4().int) % 1_000_000,
        email=f"v5pp-{uuid.uuid4().hex[:6]}@test.recipes.wisechef.ai",
        display_name="V5 Pro+ User",
        stripe_customer_id="cus_test_proplus_v5",
        subscription_id="sub_test_proplus_v5",
        subscription_status="active",
        subscription_tier="pro_plus",  # Phase 5: canonical slug
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def pro_user(db) -> User:
    user = User(
        id=uuid.uuid4(),
        github_id=2_001 + int(uuid.uuid4().int) % 1_000_000,
        email=f"v5pro-{uuid.uuid4().hex[:6]}@test.recipes.wisechef.ai",
        display_name="V5 Pro User",
        stripe_customer_id="cus_test_pro_v5",
        subscription_id="sub_test_pro_v5",
        subscription_status="active",
        subscription_tier="pro",
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

def test_pro_and_pro_plus_in_price_ids():
    """pro and pro_plus slugs must appear in TIER_PRICE_IDS (canonical slugs post-Phase 5)."""
    import importlib
    from app import subscription_service as ss
    ss = importlib.reload(ss)
    assert "pro" in ss.TIER_PRICE_IDS
    assert "pro_plus" in ss.TIER_PRICE_IDS
    assert "studio" not in ss.TIER_PRICE_IDS
    assert "cook" not in ss.TIER_PRICE_IDS
    assert "operator" not in ss.TIER_PRICE_IDS
    assert set(ss.TIER_PRICE_IDS) == {"pro", "pro_plus"}


def test_legacy_studio_shim_still_accepted_in_checkout(configured_prices, db, pro_plus_user):
    """Backwards-compat: create_checkout_session('studio') still works via shim.

    # RCP-INCIDENT-2026-05-11 backwards-compat shim test, remove after 2026-06-10
    """
    from app.subscription_service import create_checkout_session
    import stripe

    fake_customer = {"id": "cus_shim_legacy"}
    fake_session = {"id": "cs_shim_test", "url": "https://checkout.stripe.com/cs_shim"}

    pro_plus_user.stripe_customer_id = "cus_shim_legacy"
    db.commit()

    with patch("stripe.Customer.create", return_value=fake_customer), \
         patch("stripe.checkout.Session.create", return_value=fake_session), \
         patch("stripe.PromotionCode.list", return_value={"data": []}):
        result = create_checkout_session(user=pro_plus_user, tier="studio", db=db)
    assert result["tier"] == "pro_plus"  # normalised to canonical


def test_legacy_cook_shim_still_accepted_in_checkout(configured_prices, db, pro_user):
    """Backwards-compat: create_checkout_session('cook') still works via shim.

    # RCP-INCIDENT-2026-05-11 backwards-compat shim test, remove after 2026-06-10
    """
    from app.subscription_service import create_checkout_session

    fake_customer = {"id": "cus_shim_cook"}
    fake_session = {"id": "cs_shim_cook", "url": "https://checkout.stripe.com/cs_shim_cook"}

    pro_user.stripe_customer_id = "cus_shim_cook"
    db.commit()

    with patch("stripe.Customer.create", return_value=fake_customer), \
         patch("stripe.checkout.Session.create", return_value=fake_session), \
         patch("stripe.PromotionCode.list", return_value={"data": []}):
        result = create_checkout_session(user=pro_user, tier="cook", db=db)
    assert result["tier"] == "pro"  # normalised to canonical


def test_legacy_operator_shim_still_accepted_in_checkout(configured_prices, db, pro_plus_user):
    """Backwards-compat: create_checkout_session('operator') still works via shim.

    # RCP-INCIDENT-2026-05-11 backwards-compat shim test, remove after 2026-06-10
    """
    from app.subscription_service import create_checkout_session

    fake_customer = {"id": "cus_shim_op"}
    fake_session = {"id": "cs_shim_op", "url": "https://checkout.stripe.com/cs_shim_op"}

    pro_plus_user.stripe_customer_id = "cus_shim_op"
    db.commit()

    with patch("stripe.Customer.create", return_value=fake_customer), \
         patch("stripe.checkout.Session.create", return_value=fake_session), \
         patch("stripe.PromotionCode.list", return_value={"data": []}):
        result = create_checkout_session(user=pro_plus_user, tier="operator", db=db)
    assert result["tier"] == "pro_plus"  # normalised to canonical


def test_operator_restored_in_settings_defaults():
    """Settings.STRIPE_PRICE_OPERATOR field exists (kept for compat)."""
    from app.config import Settings
    assert "STRIPE_PRICE_OPERATOR" in Settings.model_fields
    assert "STRIPE_PRICE_STUDIO" in Settings.model_fields  # deprecated alias, kept for compat


def test_pro_checkout_works(configured_prices, db, pro_user):
    """create_checkout_session('pro') must succeed (canonical slug post-Phase 5)."""
    from app.subscription_service import create_checkout_session

    fake_customer = {"id": "cus_test_pro_v5"}
    fake_session = {"id": "cs_pro_test", "url": "https://checkout.stripe.com/cs_pro_test"}

    with patch("stripe.Customer.create", return_value=fake_customer), \
         patch("stripe.checkout.Session.create", return_value=fake_session), \
         patch("stripe.PromotionCode.list", return_value={"data": []}):
        result = create_checkout_session(user=pro_user, tier="pro", db=db)
    assert result["tier"] == "pro"
    assert result["session_id"] == "cs_pro_test"


def test_pro_checkout_endpoint_works(configured_prices, db, pro_user):
    """POST /api/checkout/pro returns 200 (canonical slug post-Phase 5)."""
    client = _client_for(db, pro_user)
    fake_session = {"id": "cs_ep_pro", "url": "https://checkout.stripe.com/cs_ep_pro"}
    fake_customer = {"id": "cus_ep_pro"}
    with patch("stripe.Customer.create", return_value=fake_customer), \
         patch("stripe.checkout.Session.create", return_value=fake_session), \
         patch("stripe.PromotionCode.list", return_value={"data": []}):
        resp = client.post("/api/checkout/pro")
    assert resp.status_code == 200, resp.text


def test_pro_plus_checkout_endpoint_works(configured_prices, db, pro_plus_user):
    """POST /api/checkout/pro_plus returns 200 (canonical slug post-Phase 5)."""
    client = _client_for(db, pro_plus_user)
    fake_session = {"id": "cs_ep_pp", "url": "https://checkout.stripe.com/cs_ep_pp"}
    fake_customer = {"id": "cus_ep_pp"}
    with patch("stripe.Customer.create", return_value=fake_customer), \
         patch("stripe.checkout.Session.create", return_value=fake_session), \
         patch("stripe.PromotionCode.list", return_value={"data": []}):
        resp = client.post("/api/checkout/pro_plus")
    assert resp.status_code == 200, resp.text


# ── Downgrade endpoint ───────────────────────────────────────────────────

def test_downgrade_pro_plus_to_pro_calls_stripe_modify(configured_prices, db, pro_plus_user):
    """POST /api/subscriptions/downgrade switches pro_plus→pro with proration."""
    fake_sub = {"id": pro_plus_user.subscription_id, "items": {"data": [{"id": "si_test_001"}]}}
    fake_modified = {
        "id": pro_plus_user.subscription_id,
        "status": "active",
        "items": {"data": [{
            "id": "si_test_001",
            "price": {"id": "price_test_pro_v5", "metadata": {"tier": "pro"}},
        }]},
    }
    client = _client_for(db, pro_plus_user)

    with patch("stripe.Subscription.retrieve", return_value=fake_sub) as ret_mock, \
         patch("stripe.Subscription.modify", return_value=fake_modified) as mod_mock:
        resp = client.post("/api/subscriptions/downgrade")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tier"] == "pro"
    assert body["subscription_id"] == pro_plus_user.subscription_id

    ret_mock.assert_called_once()
    mod_mock.assert_called_once()
    kwargs = mod_mock.call_args.kwargs
    assert kwargs.get("proration_behavior") == "create_prorations"
    items = kwargs.get("items") or []
    assert items and items[0].get("id") == "si_test_001"
    assert items[0].get("price") == "price_test_pro_v5"


def test_downgrade_rejects_pro_user(configured_prices, db, pro_user):
    """Only pro_plus users can downgrade — pro returns 400 with not_pro_plus."""
    client = _client_for(db, pro_user)
    resp = client.post("/api/subscriptions/downgrade")
    assert resp.status_code == 400
    assert "not_pro_plus" in resp.json()["detail"]


def test_downgrade_requires_auth(configured_prices, db):
    """Anonymous downgrade returns 401."""
    client = _client_for(db, None)
    resp = client.post("/api/subscriptions/downgrade")
    assert resp.status_code == 401
