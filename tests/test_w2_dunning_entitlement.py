"""W2 — a failed renewal must not silently become free service.

For revenue to accrue unattended, entitlement has to lapse on its own. Audit
finding: five gates read ``User.subscription_tier`` WITHOUT consulting
``User.subscription_status``. That column keeps its paid value through
``past_due``, ``unpaid``, ``incomplete`` and ``paused`` — so a customer whose
card failed kept every Pro capability indefinitely, and dunning was decorative.

``app.middleware.api_key`` and ``app.forks_routes`` already gated on status
correctly, which is why this never surfaced as a total outage — it was a
per-route hole, i.e. exactly the bug CLASS worth fixing once in
:func:`app.revenue_truth.entitled_tier` rather than five times by hand.

Also covers the third webhook property: ORDERING. Stripe delivery is
at-least-once but not ordered, and an older event delivered after a newer one
could clobber ``past_due`` back to ``active``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.models import APIKey, User

# Statuses that mean "this subscription is NOT currently paying". Every one of
# these leaves ``subscription_tier`` untouched at its paid value.
LAPSED_STATUSES = ["past_due", "unpaid", "incomplete", "incomplete_expired", "canceled", "paused", None]


def _user(db, **kwargs) -> User:
    defaults = dict(display_name="W2 Dunning", subscription_tier=None, subscription_status=None)
    defaults.update(kwargs)
    u = User(id=uuid4(), email=f"w2d-{uuid4().hex[:8]}@test.example", **defaults)
    db.add(u)
    db.flush()
    return u


def _fake_request(api_key_user_id):
    """Minimal stand-in for a Starlette Request carrying middleware auth state."""

    class _State:
        pass

    class _Req:
        pass

    state = _State()
    state.api_key_user_id = api_key_user_id
    req = _Req()
    req.state = state
    return req


# ── The shared predicate ────────────────────────────────────────────────────


class TestEntitledTier:
    def test_active_pro_is_entitled(self, db_session):
        from app.revenue_truth import entitled_tier

        u = _user(db_session, subscription_tier="pro", subscription_status="active")
        assert entitled_tier(u) == "pro"

    def test_trialing_is_entitled(self, db_session):
        from app.revenue_truth import entitled_tier

        u = _user(db_session, subscription_tier="pro", subscription_status="trialing")
        assert entitled_tier(u) == "pro"

    @pytest.mark.parametrize("status", LAPSED_STATUSES)
    def test_lapsed_pro_is_not_entitled(self, db_session, status):
        """The tier column still says 'pro'; the entitlement must not."""
        from app.revenue_truth import entitled_tier

        u = _user(db_session, subscription_tier="pro", subscription_status=status)
        assert u.subscription_tier == "pro", "precondition: the paid tier is still on the row"
        assert entitled_tier(u) is None, f"status={status!r} still entitled to Pro"

    def test_none_user_is_not_entitled(self):
        from app.revenue_truth import entitled_tier

        assert entitled_tier(None) is None

    def test_or_free_variant(self, db_session):
        from app.revenue_truth import entitled_tier_or_free

        assert entitled_tier_or_free(_user(db_session, subscription_tier="pro", subscription_status="past_due")) == "free"
        assert entitled_tier_or_free(None) == "free"


# ── The five gates that were reading the raw column ─────────────────────────


class TestDeployGateHonoursSubscriptionStatus:
    """``/api/cookbook-deploy/*`` — a value-delivery surface (ordered apply)."""

    @pytest.mark.parametrize("status", ["past_due", "unpaid", "canceled", "paused", None])
    def test_lapsed_pro_cannot_deploy(self, db_session, status):
        from app.bundle_deployment_routes import _require_deploy_tier

        u = _user(db_session, subscription_tier="pro", subscription_status=status)
        with pytest.raises(HTTPException) as exc:
            _require_deploy_tier(u)
        assert exc.value.status_code == 402, f"status={status!r} still deploying on a dead card"

    def test_active_pro_can_deploy(self, db_session):
        from app.bundle_deployment_routes import _require_deploy_tier

        u = _user(db_session, subscription_tier="pro", subscription_status="active")
        assert _require_deploy_tier(u) is u

    def test_trialing_pro_can_deploy(self, db_session):
        from app.bundle_deployment_routes import _require_deploy_tier

        u = _user(db_session, subscription_tier="pro", subscription_status="trialing")
        assert _require_deploy_tier(u) is u

    def test_anonymous_still_401(self):
        from app.bundle_deployment_routes import _require_deploy_tier

        with pytest.raises(HTTPException) as exc:
            _require_deploy_tier(None)
        assert exc.value.status_code == 401


class TestBundleQuotaGateHonoursSubscriptionStatus:
    """Bundle caps: a lapsed Pro must drop back to the FREE private-bundle cap."""

    @pytest.mark.parametrize("status", ["past_due", "unpaid", "canceled"])
    def test_lapsed_pro_gets_free_bundle_cap(self, db_session, status):
        from app.bundle_routes import require_cookbook_tier
        from app.tier_labels import bundle_limit

        u = _user(db_session, subscription_tier="pro", subscription_status=status)
        db_session.commit()

        ctx = require_cookbook_tier(_fake_request(u.id), db_session)
        assert ctx.tier == "free", f"status={status!r} kept the Pro bundle cap"
        # The cap is what actually bites (Pro=50 private bundles vs Free=2).
        assert bundle_limit(ctx.tier) == bundle_limit("free")

    def test_active_pro_keeps_pro_bundle_cap(self, db_session):
        from app.bundle_routes import require_cookbook_tier

        u = _user(db_session, subscription_tier="pro", subscription_status="active")
        db_session.commit()
        assert require_cookbook_tier(_fake_request(u.id), db_session).tier == "pro"


class TestApiKeyCapHonoursSubscriptionStatus:
    def test_lapsed_pro_gets_free_key_cap(self, db_session):
        # bundles_0811 P2.5: the KEY_CAP/DEFAULT_CAP dict literals in
        # app/api_key_routes.py were replaced by the config/tiers.yaml SSOT,
        # read through app.tier_labels.api_key_cap(). The guarantee under test
        # is unchanged: a past_due Pro is entitled to FREE, so it gets Free's
        # cap (1), not Pro's (10).
        from app.revenue_truth import entitled_tier_or_free
        from app.tier_labels import api_key_cap

        u = _user(db_session, subscription_tier="pro", subscription_status="past_due")
        tier = entitled_tier_or_free(u)
        assert api_key_cap(tier) == api_key_cap("free")
        # Pin the asymmetry too — otherwise this passes if every tier collapses
        # to one value, which is the exact defect P2.5 fixed.
        assert api_key_cap("pro") > api_key_cap("free")


class TestFleetCtxHonoursSubscriptionStatus:
    def test_lapsed_pro_plus_fleet_ctx_is_free(self, db_session):
        from app.fleet_routes import resolve_fleet_ctx

        u = _user(db_session, subscription_tier="pro_plus", subscription_status="past_due")
        db_session.commit()
        assert resolve_fleet_ctx(_fake_request(u.id), db_session).tier == "free"

    def test_active_pro_plus_fleet_ctx_keeps_tier(self, db_session):
        from app.fleet_routes import resolve_fleet_ctx

        u = _user(db_session, subscription_tier="pro_plus", subscription_status="active")
        db_session.commit()
        assert resolve_fleet_ctx(_fake_request(u.id), db_session).tier == "pro_plus"


class TestRecallTierHonoursSubscriptionStatus:
    def test_lapsed_pro_recalls_as_free(self, db_session, monkeypatch):
        """Recall's tier-visibility filter must not show Pro skills to a dead card."""
        import app.recall_routes as rr

        u = _user(db_session, subscription_tier="pro", subscription_status="unpaid")
        db_session.commit()

        seen: dict = {}

        def _fake_recall(db, **kwargs):
            seen.update(kwargs)
            return {"hits": [], "used_fallback": False, "backend": "bm25"}

        monkeypatch.setattr(rr, "recall_skills", _fake_recall)
        rr.post_recall(rr.RecallIn(query="anything"), _fake_request(u.id), db_session)
        assert seen["user_tier"] == "free", "lapsed Pro recalled with Pro visibility"

    def test_active_pro_recalls_as_pro(self, db_session, monkeypatch):
        import app.recall_routes as rr

        u = _user(db_session, subscription_tier="pro", subscription_status="active")
        db_session.commit()

        seen: dict = {}

        def _fake_recall(db, **kwargs):
            seen.update(kwargs)
            return {"hits": [], "used_fallback": False, "backend": "bm25"}

        monkeypatch.setattr(rr, "recall_skills", _fake_recall)
        rr.post_recall(rr.RecallIn(query="anything"), _fake_request(u.id), db_session)
        assert seen["user_tier"] == "pro"


class TestNoGateReadsTheRawTierColumn:
    """Source guard paired with the behavioural tests above (trap V3).

    The behavioural tests pin the five gates that exist today. This catches a
    SIXTH one added later that reaches for ``user.subscription_tier`` directly
    without a status check, which is how this class of bug got in five times.
    """

    def test_entitlement_gates_use_the_shared_predicate(self):
        import re
        from pathlib import Path

        app_dir = Path(__file__).resolve().parent.parent / "app"
        # Modules that legitimately report or mutate the raw column rather than
        # gate on it: the webhook writer, the self-reporting billing surfaces,
        # the pulse (which counts subscriptions, not entitlements), and the
        # modules that already do their own explicit status check.
        allowed = {
            "subscription_service.py",  # writes the column
            "founding_service.py",  # feat/founding — writes the column (grant), mirrors subscription_service.py
            "admin_routes.py",  # counts subscriptions, filters on status in SQL
            "auth_routes.py",  # /me self-report: shows what the row says
            "checkout_routes.py",  # billing self-report + its own _HEALTHY_STATUSES
            "forks_routes.py",  # has an explicit ACTIVE_SUB_STATUSES check
            "health_routes.py",  # ops counters
            "models.py",  # the declaration itself
            "revenue_truth.py",  # the shared predicate
            "_skill_helpers.py",  # has its own explicit status check
            "subscriber_credit_service.py",  # credit grant, checks status separately
            "role_sync.py",  # Discord role sync, checks status separately
            "api_key.py",  # middleware, gates on status correctly
            # fdeloop_0808 Ph D: extracted VERBATIM out of api_key.py to keep
            # that module under the 600-line god-object cap. It carries the same
            # inline `subscription_status in ("active","trialing")` guard the
            # allowance above was granted for — the code did not change, only
            # the file it lives in.
            "_jwt_cookie_auth.py",
            "recall_routes.py",  # routed through entitled_tier_or_free
            "fleet_routes.py",  # routed through entitled_tier_or_free
            "bundle_routes.py",  # routed through entitled_tier_or_free
            "api_key_routes.py",  # routed through entitled_tier_or_free
            "bundle_deployment_routes.py",  # routed through entitled_tier
        }
        offenders: list[str] = []
        for path in app_dir.rglob("*.py"):
            if path.name in allowed:
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for idx, line in enumerate(lines, start=1):
                # Prose about the column (comments, docstring bullets) is not a
                # gate. Only real attribute access counts.
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"\.subscription_tier\b", line):
                    offenders.append(f"{path.relative_to(app_dir.parent)}:{idx}")
        assert not offenders, (
            "these read User.subscription_tier directly — use "
            "app.revenue_truth.entitled_tier(user) so a past_due card loses "
            f"entitlement: {offenders}"
        )


# ── Webhook property 3: ordering / staleness ────────────────────────────────


def _sub_payload(user: User, *, status: str, sub_id: str = "sub_w2_order") -> dict:
    return {
        "id": sub_id,
        "status": status,
        "customer": "cus_w2_order",
        "current_period_end": 1900000000,
        "items": {
            "data": [
                {
                    "id": "si_w2",
                    "quantity": 1,
                    "price": {
                        "id": "price_pro_w2",
                        "unit_amount": 995,
                        "recurring": {"interval": "month", "interval_count": 1},
                        "metadata": {"tier": "pro"},
                    },
                }
            ]
        },
        "metadata": {"loopskill_user_id": str(user.id)},
    }


def _event(event_type: str, obj: dict, *, created: int) -> dict:
    return {
        "id": f"evt_{uuid4().hex}",
        "type": event_type,
        "livemode": False,
        "created": created,
        "data": {"object": obj},
    }


class TestWebhookOrderingGuard:
    """Stripe is at-least-once but NOT ordered — a third property, distinct from
    signature verification and from idempotency."""

    def test_older_active_event_cannot_clobber_newer_past_due(self, db_session):
        """The failure this guard exists for.

        past_due lands first (t=2000), then an OLDER active event (t=1000) is
        delivered late. Without the guard the user is active again and Pro
        entitlement is restored to a card that has already failed.
        """
        from app.subscription_service import handle_subscription_event

        u = _user(db_session, subscription_tier="pro", subscription_status="active",
                  subscription_id="sub_w2_order", stripe_customer_id="cus_w2_order")
        db_session.commit()

        handle_subscription_event(
            _event("customer.subscription.updated", _sub_payload(u, status="past_due"), created=2000),
            db_session,
        )
        db_session.refresh(u)
        assert u.subscription_status == "past_due"

        result = handle_subscription_event(
            _event("customer.subscription.updated", _sub_payload(u, status="active"), created=1000),
            db_session,
        )
        db_session.refresh(u)
        assert result.get("skipped") == "stale-event", result
        assert u.subscription_status == "past_due", "a stale event resurrected the subscription"

    def test_newer_event_still_applies(self, db_session):
        """The guard must not become a wall — newer events go through."""
        from app.subscription_service import handle_subscription_event

        u = _user(db_session, subscription_tier="pro", subscription_status="active",
                  subscription_id="sub_w2_order", stripe_customer_id="cus_w2_order")
        db_session.commit()

        handle_subscription_event(
            _event("customer.subscription.updated", _sub_payload(u, status="past_due"), created=2000),
            db_session,
        )
        handle_subscription_event(
            _event("customer.subscription.updated", _sub_payload(u, status="active"), created=3000),
            db_session,
        )
        db_session.refresh(u)
        assert u.subscription_status == "active"

    def test_stale_delete_cannot_revoke_after_resubscribe(self, db_session):
        from app.subscription_service import handle_subscription_event

        u = _user(db_session, subscription_tier="pro", subscription_status="active",
                  subscription_id="sub_w2_order", stripe_customer_id="cus_w2_order")
        db_session.commit()

        handle_subscription_event(
            _event("customer.subscription.updated", _sub_payload(u, status="active"), created=5000),
            db_session,
        )
        result = handle_subscription_event(
            _event("customer.subscription.deleted", _sub_payload(u, status="canceled"), created=4000),
            db_session,
        )
        db_session.refresh(u)
        assert result.get("skipped") == "stale-event", result
        assert u.subscription_tier == "pro"

    def test_stale_update_cannot_resurrect_after_delete(self, db_session):
        from app.subscription_service import handle_subscription_event

        u = _user(db_session, subscription_tier="pro", subscription_status="active",
                  subscription_id="sub_w2_order", stripe_customer_id="cus_w2_order")
        db_session.commit()

        handle_subscription_event(
            _event("customer.subscription.deleted", _sub_payload(u, status="canceled"), created=8000),
            db_session,
        )
        db_session.refresh(u)
        assert u.subscription_tier is None

        handle_subscription_event(
            _event("customer.subscription.updated", _sub_payload(u, status="active"), created=7000),
            db_session,
        )
        db_session.refresh(u)
        assert u.subscription_tier is None, "a stale update reinstated a cancelled subscription"
        assert u.subscription_status == "canceled"

    def test_event_without_created_is_applied(self, db_session):
        """An unorderable event is applied, not dropped — better than losing a
        real state change to a missing field."""
        from app.subscription_service import handle_subscription_event

        u = _user(db_session, subscription_tier="pro", subscription_status="active",
                  subscription_id="sub_w2_order", stripe_customer_id="cus_w2_order")
        db_session.commit()

        event = _event("customer.subscription.updated", _sub_payload(u, status="past_due"), created=2000)
        del event["created"]
        handle_subscription_event(event, db_session)
        db_session.refresh(u)
        assert u.subscription_status == "past_due"


class TestWebhookIdempotencyIsARealConstraint:
    def test_event_id_is_a_primary_key_not_an_application_check(self):
        """Replay protection must be enforced by the DATABASE.

        An application-level "have I seen this?" query races itself under
        concurrent delivery; a primary key cannot.
        """
        from app.models import StripeEventId

        pk_cols = [c.name for c in StripeEventId.__table__.primary_key.columns]
        assert pk_cols == ["event_id"], pk_cols

    def test_replay_is_skipped(self, db_session):
        from app.subscription_service import record_event_or_skip

        event = {"id": f"evt_w2_{uuid4().hex}", "type": "customer.subscription.updated", "livemode": False}
        assert record_event_or_skip(event, db_session) is True
        assert record_event_or_skip(event, db_session) is False


# ── invoice.payment_failed ──────────────────────────────────────────────────


class TestInvoicePaymentFailed:
    """A failed renewal must be recorded and alerted, not swallowed."""

    def _invoice(self, *, sub_id="sub_w2_fail", attempt=1, amount_due=995, next_attempt=None) -> dict:
        return {
            "id": "in_w2_fail",
            "customer": "cus_w2_fail",
            "subscription": sub_id,
            "attempt_count": attempt,
            "amount_due": amount_due,
            "amount_paid": 0,
            "currency": "usd",
            "next_payment_attempt": next_attempt,
            "billing_reason": "subscription_cycle",
        }

    def test_failed_payment_marks_subscription_past_due(self, db_session):
        from app.subscription_service import handle_invoice_payment_failed

        u = _user(db_session, subscription_tier="pro", subscription_status="active",
                  subscription_id="sub_w2_fail", stripe_customer_id="cus_w2_fail")
        db_session.commit()

        result = handle_invoice_payment_failed(
            _event("invoice.payment_failed", self._invoice(), created=9000), db_session
        )
        db_session.refresh(u)
        assert result["processed"] == "invoice.payment_failed", result
        assert u.subscription_status == "past_due"

    def test_past_due_removes_entitlement_at_the_gate(self, db_session):
        """The point of the whole exercise: the 402 actually happens."""
        from app.bundle_deployment_routes import _require_deploy_tier
        from app.subscription_service import handle_invoice_payment_failed

        u = _user(db_session, subscription_tier="pro", subscription_status="active",
                  subscription_id="sub_w2_fail", stripe_customer_id="cus_w2_fail")
        db_session.commit()
        assert _require_deploy_tier(u) is u  # entitled before the failure

        handle_invoice_payment_failed(
            _event("invoice.payment_failed", self._invoice(), created=9000), db_session
        )
        db_session.refresh(u)
        with pytest.raises(HTTPException) as exc:
            _require_deploy_tier(u)
        assert exc.value.status_code == 402

    def test_unknown_customer_is_skipped_not_crashed(self, db_session):
        from app.subscription_service import handle_invoice_payment_failed

        inv = self._invoice()
        inv["customer"] = "cus_does_not_exist"
        result = handle_invoice_payment_failed(
            _event("invoice.payment_failed", inv, created=9000), db_session
        )
        assert result.get("skipped") == "user-not-found"

    def test_one_off_invoice_does_not_touch_subscription_state(self, db_session):
        """An invoice with no subscription is not a renewal — leave the tier alone."""
        from app.subscription_service import handle_invoice_payment_failed

        u = _user(db_session, subscription_tier="pro", subscription_status="active",
                  subscription_id="sub_w2_fail", stripe_customer_id="cus_w2_fail")
        db_session.commit()

        inv = self._invoice()
        inv["subscription"] = None
        result = handle_invoice_payment_failed(
            _event("invoice.payment_failed", inv, created=9000), db_session
        )
        db_session.refresh(u)
        assert result.get("skipped") == "non-subscription-invoice"
        assert u.subscription_status == "active"

    def test_stale_failure_does_not_clobber_recovered_subscription(self, db_session):
        """A late-delivered failure must not un-recover a paid-up subscription."""
        from app.subscription_service import handle_invoice_payment_failed, handle_subscription_event

        u = _user(db_session, subscription_tier="pro", subscription_status="past_due",
                  subscription_id="sub_w2_fail", stripe_customer_id="cus_w2_fail")
        db_session.commit()

        # Recovery lands at t=9000.
        handle_subscription_event(
            _event("customer.subscription.updated",
                   _sub_payload(u, status="active", sub_id="sub_w2_fail"), created=9000),
            db_session,
        )
        db_session.refresh(u)
        assert u.subscription_status == "active"

        # The older failure is delivered afterwards.
        result = handle_invoice_payment_failed(
            _event("invoice.payment_failed", self._invoice(), created=8000), db_session
        )
        db_session.refresh(u)
        assert result.get("skipped") == "stale-event", result
        assert u.subscription_status == "active"

    def test_webhook_route_dispatches_payment_failed(self):
        """The handler must actually be wired into the webhook router."""
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "app" / "creator_routes.py").read_text()
        assert "invoice.payment_failed" in src, "payment_failed is handled but never dispatched"
        assert "handle_invoice_payment_failed" in src


class TestRevocationAtValueDelivery:
    """``customer.subscription.deleted`` must revoke where value is DELIVERED."""

    def test_cancel_revokes_the_deploy_gate(self, db_session):
        from app.bundle_deployment_routes import _require_deploy_tier
        from app.subscription_service import handle_subscription_event

        u = _user(db_session, subscription_tier="pro", subscription_status="active",
                  subscription_id="sub_w2_order", stripe_customer_id="cus_w2_order")
        db_session.commit()
        assert _require_deploy_tier(u) is u

        handle_subscription_event(
            _event("customer.subscription.deleted", _sub_payload(u, status="canceled"), created=9999),
            db_session,
        )
        db_session.refresh(u)
        with pytest.raises(HTTPException) as exc:
            _require_deploy_tier(u)
        assert exc.value.status_code == 402

    @pytest.mark.parametrize("status", ["past_due", "unpaid", "canceled", "paused"])
    def test_keyed_request_loses_its_tier_when_the_subscription_lapses(
        self, db_session, monkeypatch, status
    ):
        """The tier stamped on EVERY keyed request drops, so the tarball mint and
        every other x-api-key surface sees Free immediately — revocation at the
        point of value delivery, not just in the DB.

        ``_auth_ctx_from_api_key`` opens its own ``SessionLocal``, which cannot
        see rows inside the test's uncommitted transaction, so the factory is
        pointed at the test session (with ``close()`` neutralised, since the
        production code closes the session it opened).
        """
        import hashlib

        import app.database as database
        from app.middleware.api_key import _auth_ctx_from_api_key

        raw_key = f"rec_w2{uuid4().hex}"
        u = _user(db_session, subscription_tier="pro", subscription_status="active")
        db_session.add(
            APIKey(
                id=uuid4(),
                user_id=u.id,
                key_prefix=raw_key[:12],
                key_hash=hashlib.sha256(raw_key.encode()).hexdigest(),
                name="w2",
                is_active=True,
            )
        )
        db_session.commit()

        class _NoCloseSession:
            """Proxy the test session but ignore close() from production code."""

            def __getattr__(self, name):
                return getattr(db_session, name)

            def close(self):
                pass

        monkeypatch.setattr(database, "SessionLocal", lambda: _NoCloseSession())

        class _Req:
            headers = {"x-api-key": raw_key}

        ctx = _auth_ctx_from_api_key(_Req())
        assert ctx is not None and ctx.tier == "pro", "precondition: active Pro key resolves to Pro"

        u.subscription_status = status
        db_session.commit()

        ctx = _auth_ctx_from_api_key(_Req())
        assert ctx is not None, "identity must survive — only the TIER lapses"
        assert ctx.user_id == u.id
        assert ctx.tier is None, f"status={status!r} kept Pro entitlement on every keyed request"

    def test_cancel_clears_the_tier_column_outright(self, db_session):
        from app.subscription_service import handle_subscription_event

        u = _user(db_session, subscription_tier="pro", subscription_status="active",
                  subscription_id="sub_w2_order", stripe_customer_id="cus_w2_order")
        db_session.commit()

        handle_subscription_event(
            _event("customer.subscription.deleted", _sub_payload(u, status="canceled"), created=9999),
            db_session,
        )
        db_session.refresh(u)
        assert u.subscription_tier is None
        assert u.subscription_status == "canceled"
