"""perclient_1110 M2.3 — a paid Pro webhook lands in the advertised entitlement.

The pieces of this chain were each pinned on their own:

* webhook -> ``subscription_tier`` / ``subscription_status`` (test_subscription.py),
* ``bundle_limit("pro") == 50`` (test_m1_private_bundle_meter.py, test_tier_labels.py),
* ``api_key_cap("pro") == 10`` (test_bundles0811_p25_api_key_cap_ssot.py),

but no single test walked a buyer from the Stripe event to the two numbers the
pricing page promises. A break in the hand-off (tier resolved from the price
id, status gate, cap lookup) could leave every link green while a paying
stranger is still held to Free limits. This suite is that end-to-end pin:

1. ``checkout.session.completed`` (subscription mode, paid) for the Pro price
   -> user is ``pro`` / ``active``.
2. The SAME user, through the real REST enforcers, can own 50 private bundles
   (the 51st is refused with ``max_private_bundles == 50``) and mint 10 API keys
   (the 11th is refused with ``key_cap_exceeded``).
3. The ``customer.subscription.created`` path reaches the same entitlement.
4. Negative control: a subscription that never became ``active`` (``incomplete``)
   stays on Free limits — so the positive assertions cannot pass vacuously.

The numbers 50 and 10 are written as literals on purpose: they are the
customer-facing promise, and config/tiers.yaml must not drift away from it
without this test failing.

In-memory SQLite, Stripe calls patched — no network, no prod DB.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.database import get_db
from app.models import Base, Bundle, User

PRO_PRICE_ID = "price_test_perclient_pro"
PRO_PLUS_PRICE_ID = "price_test_perclient_pro_plus"

# The customer-facing promise (pricing page / config/tiers.yaml `pro`).
PRO_PRIVATE_BUNDLES = 50
PRO_API_KEYS = 10
FREE_PRIVATE_BUNDLES = 2
FREE_API_KEYS = 1


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine_fixture():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(conn, _record):
        conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def db(engine_fixture) -> Generator[Session, None, None]:
    connection = engine_fixture.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, autocommit=False, autoflush=False)()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture()
def pro_prices(monkeypatch):
    """Point the Pro / Pro+ price ids at test values (reverse-lookup source)."""
    from app import subscription_service as ss

    monkeypatch.setattr(settings, "STRIPE_PRICE_PRO", PRO_PRICE_ID)
    monkeypatch.setattr(settings, "STRIPE_PRICE_PRO_PLUS", PRO_PLUS_PRICE_ID)
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
    monkeypatch.setattr(ss, "TIER_PRICE_IDS", {"pro": PRO_PRICE_ID, "pro_plus": PRO_PLUS_PRICE_ID})
    yield


@pytest.fixture()
def buyer(db) -> User:
    """A signed-up user who has not paid yet (no tier, no status)."""
    uid = uuid4()
    user = User(
        id=uid,
        display_name="perclient_1110 buyer",
        email=f"pc1110-{uid.hex[:8]}@test.example",
        subscription_tier=None,
        subscription_status=None,
    )
    db.add(user)
    db.commit()
    return user


def _build_app(db: Session, user_id) -> FastAPI:
    """Webhook + bundle + API-key routers, all on one in-memory DB.

    The bundle enforcer authenticates through ``request.state.api_key_user_id``
    (set by the API-key middleware in prod); the API-key routes through
    ``get_current_user_optional``. Both are re-read from the DB on every
    request, so they see whatever the webhook just wrote.
    """
    from app.api_key_routes import router as api_key_router
    from app.auth_routes import get_current_user_optional
    from app.bundle_routes import router as bundle_router
    from app.creator_routes import router as creator_router

    app = FastAPI()

    def _override_get_db():
        yield db

    def _override_user():
        return db.query(User).filter(User.id == user_id).first()

    class InjectAuthState(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            request.state.api_key_user_id = user_id
            request.state.api_key_id = None
            return await call_next(request)

    app.add_middleware(InjectAuthState)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user_optional] = _override_user
    app.include_router(creator_router)
    app.include_router(bundle_router)
    app.include_router(api_key_router)
    return app


def _post_event(client: TestClient, evt: dict):
    with patch("app.creator_routes.verify_webhook_signature", return_value=evt):
        return client.post(
            "/api/stripe/webhook",
            content=json.dumps(evt).encode(),
            headers={"stripe-signature": "t=1,v1=test"},
        )


def _fake_subscription(user: User, *, status: str = "active", price_id: str = PRO_PRICE_ID) -> dict:
    """A Stripe Subscription as the live Pro price produces it.

    No ``metadata.tier`` on the price: the tier must be resolved from the price
    id itself, which is the path a real Pro checkout takes when the price
    object carries no tier metadata.
    """
    return {
        "id": f"sub_test_{uuid.uuid4().hex[:8]}",
        "object": "subscription",
        "status": status,
        "current_period_end": int((datetime.now(UTC) + timedelta(days=30)).timestamp()),
        "items": {
            "data": [{"price": {"id": price_id, "unit_amount": 995, "currency": "usd", "metadata": {}}}]
        },
        "metadata": {"loopskill_user_id": str(user.id)},
        "customer": f"cus_test_{uuid.uuid4().hex[:8]}",
        "discount": None,
        "discounts": [],
    }


def _seed_private_bundles(db: Session, owner: User, n: int) -> None:
    for _ in range(n):
        db.add(
            Bundle(
                id=uuid4(),
                name=f"client-{uuid4().hex[:8]}",
                bundle_owner=owner.id,
                slug=f"client-{uuid4().hex[:12]}",
                visibility="private",
            )
        )
    db.commit()


def _assert_entitlement(client: TestClient, db: Session, user: User, *, bundles: int, keys: int) -> None:
    """Exercise the real enforcers up to and one past each cap."""
    # Private bundles: fill to cap-1 directly, create the last one through the
    # route, then the next one must be refused with the cap in the payload.
    _seed_private_bundles(db, user, bundles - 1)
    last = client.post("/api/cookbooks", json={"name": "last allowed private bundle"})
    assert last.status_code == 201, f"bundle #{bundles} should be allowed: {last.text}"
    over = client.post("/api/cookbooks", json={"name": "one too many"})
    assert over.status_code == 403, f"bundle #{bundles + 1} must be refused: {over.text}"
    assert over.json()["detail"]["max_private_bundles"] == bundles

    # API keys: mint up to the cap through the route, the next one is refused.
    for i in range(keys):
        r = client.post("/api/api-keys", json={"label": f"client-{i}"})
        assert r.status_code == 200, f"api key #{i + 1} should be allowed: {r.text}"
    r = client.post("/api/api-keys", json={"label": "one too many"})
    assert r.status_code == 403, r.text
    assert "key_cap_exceeded" in r.json()["detail"]
    assert f"max {keys} " in r.json()["detail"]


# ── Tests ───────────────────────────────────────────────────────────────────


def test_checkout_completed_for_pro_price_grants_50_bundles_and_10_keys(db, buyer, pro_prices):
    client = TestClient(_build_app(db, buyer.id))
    sub = _fake_subscription(buyer)
    evt = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "checkout.session.completed",
        "livemode": False,
        "created": int(datetime.now(UTC).timestamp()),
        "data": {
            "object": {
                "id": f"cs_test_{uuid.uuid4().hex[:8]}",
                "object": "checkout.session",
                "mode": "subscription",
                "payment_status": "paid",
                "customer": sub["customer"],
                "subscription": sub["id"],
                "metadata": {"loopskill_user_id": str(buyer.id), "tier": "pro"},
            }
        },
    }

    with patch("stripe.Subscription.retrieve", return_value=sub):
        resp = _post_event(client, evt)

    assert resp.status_code == 200, resp.text
    assert resp.json().get("processed") == "checkout.session.completed", resp.json()

    db.expire_all()
    user = db.query(User).filter(User.id == buyer.id).one()
    assert user.subscription_tier == "pro"
    assert user.subscription_status == "active"
    assert user.subscription_id == sub["id"]

    _assert_entitlement(client, db, user, bundles=PRO_PRIVATE_BUNDLES, keys=PRO_API_KEYS)


def test_subscription_created_event_grants_the_same_pro_entitlement(db, buyer, pro_prices):
    client = TestClient(_build_app(db, buyer.id))
    sub = _fake_subscription(buyer)
    evt = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "customer.subscription.created",
        "livemode": False,
        "created": int(datetime.now(UTC).timestamp()),
        "data": {"object": sub},
    }

    resp = _post_event(client, evt)
    assert resp.status_code == 200, resp.text

    db.expire_all()
    user = db.query(User).filter(User.id == buyer.id).one()
    assert (user.subscription_tier, user.subscription_status) == ("pro", "active")

    _assert_entitlement(client, db, user, bundles=PRO_PRIVATE_BUNDLES, keys=PRO_API_KEYS)


def test_incomplete_pro_subscription_stays_on_free_limits(db, buyer, pro_prices):
    """Negative control: Pro price, but the payment never cleared."""
    client = TestClient(_build_app(db, buyer.id))
    sub = _fake_subscription(buyer, status="incomplete")
    evt = {
        "id": f"evt_{uuid.uuid4().hex}",
        "type": "customer.subscription.created",
        "livemode": False,
        "created": int(datetime.now(UTC).timestamp()),
        "data": {"object": sub},
    }

    resp = _post_event(client, evt)
    assert resp.status_code == 200, resp.text

    db.expire_all()
    user = db.query(User).filter(User.id == buyer.id).one()
    assert user.subscription_status == "incomplete"
    # The paid tier IS stored; only the status gate keeps it on Free limits.
    assert user.subscription_tier == "pro"

    _assert_entitlement(client, db, user, bundles=FREE_PRIVATE_BUNDLES, keys=FREE_API_KEYS)
