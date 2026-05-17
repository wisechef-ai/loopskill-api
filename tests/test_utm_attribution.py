"""Tests for marketing_1205 UTM ref attribution (Phase 2).

Covers:
  (a) Cookie set when ?ref=li on /api/skills/install
  (b) Cookie NOT set when ?ref=evil (unknown ref silently dropped)
  (c) /x/<slug> redirects to /api/skills/install?slug=<slug>&ref=x
  (d) customer.subscription.created webhook picks up utm_ref from sub metadata
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Base, Skill, SkillVersion, User
from tests.conftest import make_skill


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture()
def utm_client(db_session: Session):
    """TestClient that includes the routes router (UTM helper + install + platform redirects)."""
    from app.config import settings

    test_app = FastAPI()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    from app.routes import router as core_router
    from app.routes import utm_router
    from app.checkout_routes import router as checkout_router
    from app.creator_routes import router as creator_router

    test_app.include_router(core_router)
    test_app.include_router(utm_router)
    test_app.include_router(checkout_router)
    test_app.include_router(creator_router)

    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(
        test_app,
        headers={"x-api-key": settings.API_KEY},
        raise_server_exceptions=True,
    ) as c:
        yield c


def _make_skill_with_version(db: Session, slug: str = "test-utm-skill") -> Skill:
    from app.models import SkillVersion
    skill = Skill(
        id=uuid4(),
        slug=slug,
        title="UTM Test Skill",
        category="devops",
        is_public=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(skill)
    db.flush()

    version = SkillVersion(
        id=uuid4(),
        skill_id=skill.id,
        semver="1.0.0",
        tarball_path="/tmp/fake.tar.gz",
        tarball_size_bytes=1024,
        checksum_sha256="abc123",
        created_at=datetime.now(timezone.utc),
    )
    db.add(version)
    db.flush()
    return skill


def _make_user_for_utm(db: Session) -> User:
    user = User(
        id=uuid4(),
        display_name="UTM Test User",
        email="utm@example.com",
        github_id=77777,
    )
    db.add(user)
    db.flush()
    return user


# ── Test (a): cookie is set when ?ref=li ─────────────────────────────────

class TestUTMCookieSet:
    @patch("itsdangerous.URLSafeTimedSerializer.dumps", return_value="fake-token")
    def test_cookie_set_on_valid_ref(self, mock_dumps, utm_client, db_session):
        """(a) GET /api/skills/install?slug=X&ref=li → 200 with recipes_utm_ref=li cookie."""
        _make_skill_with_version(db_session, slug="super-memory")

        resp = utm_client.get(
            "/api/skills/install?slug=super-memory&ref=li",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # Cookie must be present with value 'li'
        cookies = resp.cookies
        assert "recipes_utm_ref" in cookies, f"Cookie not set. Cookies: {dict(cookies)}"
        assert cookies["recipes_utm_ref"] == "li"

    @patch("itsdangerous.URLSafeTimedSerializer.dumps", return_value="fake-token")
    def test_all_allowlisted_refs_set_cookie(self, mock_dumps, utm_client, db_session):
        """All allowlisted refs set the cookie."""
        _make_skill_with_version(db_session, slug="multi-ref-skill")

        for ref in ("li", "x", "yt", "ig", "fb", "agentpact"):
            resp = utm_client.get(
                f"/api/skills/install?slug=multi-ref-skill&ref={ref}",
                follow_redirects=False,
            )
            assert resp.status_code == 200
            assert resp.cookies.get("recipes_utm_ref") == ref, (
                f"Expected cookie for ref={ref}"
            )


# ── Test (b): cookie NOT set when ?ref=evil ───────────────────────────────

class TestUTMCookieSilentDrop:
    @patch("itsdangerous.URLSafeTimedSerializer.dumps", return_value="fake-token")
    def test_unknown_ref_no_cookie(self, mock_dumps, utm_client, db_session):
        """(b) GET /api/skills/install?slug=X&ref=evil → 200, no cookie set."""
        _make_skill_with_version(db_session, slug="evil-ref-skill")

        resp = utm_client.get(
            "/api/skills/install?slug=evil-ref-skill&ref=evil",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "recipes_utm_ref" not in resp.cookies, (
            f"Cookie should NOT be set for unknown ref. Cookies: {dict(resp.cookies)}"
        )

    @patch("itsdangerous.URLSafeTimedSerializer.dumps", return_value="fake-token")
    def test_no_ref_no_cookie(self, mock_dumps, utm_client, db_session):
        """No ?ref param → no cookie."""
        _make_skill_with_version(db_session, slug="no-ref-skill")

        resp = utm_client.get(
            "/api/skills/install?slug=no-ref-skill",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "recipes_utm_ref" not in resp.cookies


# ── Test (c): /x/<slug> redirects to /api/skills/install?...&ref=x ───────

class TestPlatformRedirects:
    """marketing_1205 — the /x/, /li/, /ig/, /yt/, /fb/ shortcut redirects.

    PR #85 changed the destination from /api/skills/install (install endpoint)
    to /skills/<slug>?ref=<platform> (public skill PAGE) — this gives social
    visitors the marketing page first so they can browse before installing,
    while still attributing them via the UTM cookie set by the redirector.
    """

    def test_x_slug_redirects(self, utm_client):
        """GET /x/<slug> → 302 to /skills/<slug>?ref=x (marketing landing)."""
        resp = utm_client.get("/x/super-memory", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "/skills/super-memory" in location
        assert "ref=x" in location

    def test_li_slug_redirects(self, utm_client):
        resp = utm_client.get("/li/some-skill", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "/skills/some-skill" in location
        assert "ref=li" in location

    def test_ig_slug_redirects(self, utm_client):
        resp = utm_client.get("/ig/my-skill", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "ref=ig" in location

    def test_yt_slug_redirects(self, utm_client):
        resp = utm_client.get("/yt/my-skill", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "ref=yt" in location

    def test_fb_slug_redirects(self, utm_client):
        resp = utm_client.get("/fb/my-skill", follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("location", "")
        assert "ref=fb" in location


# ── Test (d): webhook handler writes utm_ref from subscription metadata ───

class TestWebhookUTMRef:
    """(d) customer.subscription.created picks up utm_ref from sub.metadata."""

    def _make_sub_event(self, event_id: str, user: User, utm_ref: str | None = "li"):
        sub_meta: dict = {
            "wiserecipes_user_id": str(user.id),
            "tier": "pro",
        }
        if utm_ref:
            sub_meta["utm_ref"] = utm_ref
        return {
            "id": event_id,
            "type": "customer.subscription.created",
            "livemode": False,
            "data": {
                "object": {
                    "id": f"sub_{event_id}",
                    "status": "active",
                    "customer": user.stripe_customer_id or "cus_test",
                    "current_period_end": 9999999999,
                    "items": {
                        "data": [
                            {"price": {"id": "price_pro", "metadata": {"tier": "pro"}}}
                        ]
                    },
                    "metadata": sub_meta,
                }
            },
        }

    @patch("app.subscription_service.stripe")
    def test_webhook_sets_utm_ref_on_user(self, mock_stripe, utm_client, db_session):
        """Webhook with utm_ref in sub metadata → user.utm_ref persisted."""
        from app.models import StripeEventId

        user = _make_user_for_utm(db_session)
        user.stripe_customer_id = "cus_test_utm_001"
        db_session.flush()

        event = self._make_sub_event("evt_utm_001", user, utm_ref="li")
        mock_stripe.Webhook.construct_event.return_value = event

        # Patch verify_webhook_signature used inside creator_routes
        with patch("app.creator_routes.verify_webhook_signature", return_value=event):
            resp = utm_client.post(
                "/api/stripe/webhook",
                content=b"{}",
                headers={"stripe-signature": "sig_test"},
            )

        assert resp.status_code == 200

        db_session.refresh(user)
        assert user.utm_ref == "li", f"Expected utm_ref='li', got {user.utm_ref!r}"

    @patch("app.subscription_service.stripe")
    def test_webhook_no_utm_ref_leaves_field_null(self, mock_stripe, utm_client, db_session):
        """Webhook without utm_ref in metadata → user.utm_ref stays NULL."""
        user = _make_user_for_utm(db_session)
        user.email = "utm2@example.com"
        user.github_id = 77778
        user.stripe_customer_id = "cus_test_utm_002"
        db_session.flush()

        event = self._make_sub_event("evt_utm_002", user, utm_ref=None)

        with patch("app.creator_routes.verify_webhook_signature", return_value=event):
            resp = utm_client.post(
                "/api/stripe/webhook",
                content=b"{}",
                headers={"stripe-signature": "sig_test"},
            )

        assert resp.status_code == 200

        db_session.refresh(user)
        assert user.utm_ref is None
