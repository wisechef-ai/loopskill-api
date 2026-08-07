"""W2 — phantom-MRR: revenue alerts must report REAL cash, never list price.

Ground truth probed live 2026-08-07: all 7 "active" subscriptions are 100%
comped (every Stripe invoice amount_paid = 0.00, lifetime revenue $0.00), yet
``handle_checkout_completed`` posted "MRR impact: $9.95/mo" to Discord for each
one, because it read ``TIER_USD_PRICE[tier]`` — the LIST price of the tier — and
called it revenue.

Every assertion here is on the **RENDERED PAYLOAD** — the JSON body actually
handed to Discord's HTTP endpoint — not on the arguments passed to
``post_revenue_event`` (trap R4: verify at the destination, never at the
source's own inputs). The dispatch normally happens on a daemon thread; the
``captured_discord_payloads`` fixture runs it inline so the body is observable.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models import User

# ── Fixtures: Stripe object builders ────────────────────────────────────────


def _sub(
    *,
    unit_amount: int | None,
    sub_id: str = "sub_w2_test",
    tier: str = "pro",
    status: str = "active",
    percent_off=None,
    amount_off=None,
    coupon_in_array: bool = False,
    interval: str = "month",
    quantity: int = 1,
) -> dict:
    """A Stripe Subscription dict shaped like the real expanded response."""
    sub: dict = {
        "id": sub_id,
        "status": status,
        "customer": "cus_w2_test",
        "current_period_end": 1900000000,
        "items": {
            "data": [
                {
                    "id": "si_w2_test",
                    "quantity": quantity,
                    "price": {
                        "id": f"price_{tier}_w2",
                        "unit_amount": unit_amount,
                        "recurring": {"interval": interval, "interval_count": 1},
                        "metadata": {"tier": tier},
                    },
                }
            ]
        },
        "metadata": {},
    }
    if percent_off is not None or amount_off is not None:
        coupon = {"id": "co_w2", "percent_off": percent_off, "amount_off": amount_off}
        discount = {"id": "di_w2", "coupon": coupon}
        # Stripe exposes the subscription coupon as BOTH a singular `discount`
        # object and a `discounts` array depending on API version. Exercise
        # each shape — a handler that only reads one would miss the other and
        # fall back to reporting list price as revenue.
        if coupon_in_array:
            sub["discounts"] = [discount]
        else:
            sub["discount"] = discount
    return sub


def _checkout_session(user: User, *, sub_id: str = "sub_w2_test") -> dict:
    return {
        "id": "cs_w2_test",
        "mode": "subscription",
        "payment_status": "paid",
        "customer": "cus_w2_test",
        "subscription": sub_id,
        "metadata": {"loopskill_user_id": str(user.id)},
    }


def _event(event_type: str, obj: dict, *, event_id: str | None = None, created: int = 1_800_000_000) -> dict:
    return {
        "id": event_id or f"evt_{uuid4().hex}",
        "type": event_type,
        "livemode": False,
        "created": created,
        "data": {"object": obj},
    }


def _user(db, **kwargs) -> User:
    defaults = dict(
        display_name="W2 Tester",
        subscription_status=None,
        subscription_tier=None,
        stripe_customer_id="cus_w2_test",
    )
    defaults.update(kwargs)
    u = User(id=uuid4(), email=f"w2-{uuid4().hex[:8]}@test.example", **defaults)
    db.add(u)
    db.flush()
    return u


# ── The destination capture ─────────────────────────────────────────────────


@pytest.fixture
def captured_discord_payloads(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Capture the JSON bodies actually POSTed to the Discord webhook.

    Three things are neutralised so the payload is observable and deterministic:
      * the webhook URL env var is set, so dispatch is not a silent no-op;
      * ``threading.Thread`` runs its target inline (the real code fires and
        forgets on a daemon thread, which a test cannot join reliably);
      * ``httpx.Client`` is faked so nothing leaves the machine.
    """
    monkeypatch.setenv("RECIPES_REVENUE_WEBHOOK_URL", "https://discord.example/api/webhooks/w2/test")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("RECIPES_REVENUE_CHANNEL_ID", raising=False)

    bodies: list[dict] = []

    class _InlineThread:
        def __init__(self, *, target, args=(), **_kwargs):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)

    def _post(_url, json=None, **_kw):  # noqa: A002 — mirrors httpx's kwarg name
        bodies.append(json)
        return MagicMock(status_code=204, text="")

    fake_client.post = _post

    monkeypatch.setattr(threading, "Thread", _InlineThread)
    monkeypatch.setattr("app.revenue_alerts.httpx.Client", lambda **_kw: fake_client)
    return bodies


def _mrr_field(payload: dict) -> str:
    """The rendered 'MRR impact' field value from a Discord embed payload."""
    fields = payload["embeds"][0]["fields"]
    matches = [f["value"] for f in fields if f["name"] == "MRR impact"]
    assert matches, f"no 'MRR impact' field in rendered payload: {json.dumps(payload)}"
    return matches[0]


def _rendered_text(payload: dict) -> str:
    """Everything a human would read in the Discord message, flattened."""
    return json.dumps(payload)


# ── Deliverable 1: comped / paid / discounted, on the rendered payload ──────


class TestCheckoutCompletedRendersRealCash:
    """``handle_checkout_completed`` → the Discord body Stripe's $0.00 produced."""

    def _run(self, db, sub: dict, captured: list[dict]) -> dict:
        from app import subscription_service as ss

        user = _user(db)
        db.commit()
        event = _event("checkout.session.completed", _checkout_session(user))
        with patch.object(ss.stripe.Subscription, "retrieve", return_value=sub):
            ss.handle_checkout_completed(event, db)
        assert captured, "no revenue alert was dispatched — the pipe must stay alive"
        return captured[-1]

    def test_comped_sub_renders_zero_not_list_price(self, db_session, captured_discord_payloads):
        """A 100%-comped $9.95 activation must render $0.00, labelled comped.

        This is the live production case: 7/7 subscriptions. Before the fix the
        rendered body said "$9.95/mo".
        """
        payload = self._run(
            db_session,
            _sub(unit_amount=995, tier="pro", percent_off=100),
            captured_discord_payloads,
        )
        mrr = _mrr_field(payload)

        assert "$0.00/mo" in mrr, f"comped sub did not render $0.00: {mrr!r}"
        assert "comped" in mrr.lower(), f"comped sub not labelled comped: {mrr!r}"
        # Both figures shown, clearly distinguished.
        assert "$9.95/mo" in mrr, f"list price not shown alongside real: {mrr!r}"
        assert "real" in mrr.lower() and "list" in mrr.lower(), mrr
        # The alert still fires — the team wants to see the pipe is alive.
        assert "Subscription" in payload["embeds"][0]["title"]

    def test_comped_via_discounts_array_also_renders_zero(self, db_session, captured_discord_payloads):
        """The `discounts` array shape must be read too, not just `discount`."""
        payload = self._run(
            db_session,
            _sub(unit_amount=995, tier="pro", percent_off=100, coupon_in_array=True),
            captured_discord_payloads,
        )
        assert "$0.00/mo" in _mrr_field(payload)
        assert "comped" in _mrr_field(payload).lower()

    def test_genuine_paid_sub_renders_the_real_amount(self, db_session, captured_discord_payloads):
        """A full-price $9.95 sub renders $9.95 with no comped/discount noise."""
        payload = self._run(
            db_session, _sub(unit_amount=995, tier="pro"), captured_discord_payloads
        )
        mrr = _mrr_field(payload)
        assert mrr == "$9.95/mo", mrr
        assert "comped" not in _rendered_text(payload).lower()

    def test_discounted_sub_renders_discounted_amount_not_list(
        self, db_session, captured_discord_payloads
    ):
        """A 50%-off $9.95 sub renders $4.98 (the discounted amount), not $9.95."""
        payload = self._run(
            db_session,
            _sub(unit_amount=995, tier="pro", percent_off=50),
            captured_discord_payloads,
        )
        mrr = _mrr_field(payload)
        # 995 cents * 50% = 497.5 cents → $4.98 (half-up).
        assert "$4.98/mo" in mrr, mrr
        assert "$9.95/mo" in mrr, f"list price must still be shown for contrast: {mrr!r}"
        assert "50% off" in mrr, mrr
        # The MRR figure must not be the bare list price.
        assert mrr != "$9.95/mo"

    def test_amount_off_coupon_renders_net(self, db_session, captured_discord_payloads):
        """A flat $3.00-off coupon on $9.95 renders $6.95."""
        payload = self._run(
            db_session,
            _sub(unit_amount=995, tier="pro", amount_off=300),
            captured_discord_payloads,
        )
        assert "$6.95/mo" in _mrr_field(payload)

    def test_pro_plus_full_price_renders_hundred(self, db_session, captured_discord_payloads):
        payload = self._run(
            db_session, _sub(unit_amount=10000, tier="pro_plus"), captured_discord_payloads
        )
        assert _mrr_field(payload) == "$100.00/mo"

    def test_unresolvable_amount_is_labelled_unknown_never_list_price(
        self, db_session, captured_discord_payloads
    ):
        """No priced line item → 'unknown', NOT a confident $0 and NOT list price.

        "Stripe says this bills $0.00" is a fact; "we could not read the amount"
        is not. Rendering the second as the first is how a missing object
        becomes a number someone trusts.
        """
        payload = self._run(
            db_session, _sub(unit_amount=None, tier="pro"), captured_discord_payloads
        )
        mrr = _mrr_field(payload)
        assert "unknown" in mrr.lower(), mrr
        # It may quote the tier ceiling, but must never present it as revenue.
        assert "$0.00/mo" not in mrr, f"unknown rendered as a hard $0.00: {mrr!r}"
        if "9.95" in mrr:
            assert "ceiling" in mrr.lower() or "not revenue" in mrr.lower(), mrr


class TestNoListPriceSubstitutionAnywhere:
    """Audit: no ``post_revenue_event`` caller may substitute list price."""

    def test_cancel_alert_reports_real_churned_cash(self, db_session, captured_discord_payloads):
        """Cancelling a COMPED sub churns $0.00/mo, not the tier's list price."""
        from app import subscription_service as ss

        user = _user(db_session, subscription_status="active", subscription_tier="pro",
                     subscription_id="sub_w2_cancel")
        db_session.commit()
        sub = _sub(unit_amount=995, sub_id="sub_w2_cancel", tier="pro", percent_off=100)
        sub["metadata"] = {"loopskill_user_id": str(user.id)}
        ss.handle_subscription_event(_event("customer.subscription.deleted", sub), db_session)

        assert captured_discord_payloads, "cancel must still alert"
        mrr = _mrr_field(captured_discord_payloads[-1])
        assert "$0.00/mo" in mrr, f"comped churn reported as real revenue loss: {mrr!r}"
        assert "comped" in mrr.lower(), mrr

    def test_upgrade_alert_reports_real_new_cash(self, db_session, captured_discord_payloads):
        """A comped Pro→Pro+ upgrade is $0.00/mo of new cash, not $100."""
        from app import subscription_service as ss

        user = _user(db_session, subscription_status="active", subscription_tier="pro",
                     subscription_id="sub_w2_up")
        db_session.commit()
        sub = _sub(unit_amount=10000, sub_id="sub_w2_up", tier="pro_plus", percent_off=100)
        sub["metadata"] = {"loopskill_user_id": str(user.id)}
        ss.handle_subscription_event(_event("customer.subscription.updated", sub), db_session)

        assert captured_discord_payloads, "tier change must still alert"
        mrr = _mrr_field(captured_discord_payloads[-1])
        assert "$0.00/mo" in mrr, f"comped upgrade reported as $100 of new MRR: {mrr!r}"
        assert "$100.00/mo" in mrr, f"list price must be shown for contrast: {mrr!r}"

    def test_paid_upgrade_reports_real_amount(self, db_session, captured_discord_payloads):
        from app import subscription_service as ss

        user = _user(db_session, subscription_status="active", subscription_tier="pro",
                     subscription_id="sub_w2_up2")
        db_session.commit()
        sub = _sub(unit_amount=10000, sub_id="sub_w2_up2", tier="pro_plus")
        sub["metadata"] = {"loopskill_user_id": str(user.id)}
        ss.handle_subscription_event(_event("customer.subscription.updated", sub), db_session)
        assert _mrr_field(captured_discord_payloads[-1]) == "$100.00/mo"

    def test_post_revenue_event_rejects_a_bare_amount(self):
        """The ambiguous ``amount_usd`` parameter is GONE.

        A single "amount" is what let list price masquerade as revenue. The API
        now forces the caller to say which number it has, so the substitution
        cannot be reintroduced by accident.
        """
        import inspect

        from app.revenue_alerts import post_revenue_event

        params = inspect.signature(post_revenue_event).parameters
        assert "amount_usd" not in params, (
            "post_revenue_event still accepts an ambiguous amount_usd — a caller "
            "can pass list price and it will render as revenue"
        )
        assert "real_usd" in params and "list_usd" in params, sorted(params)

    def test_no_caller_passes_a_tier_price_table_as_the_real_amount(self):
        """Source guard, paired with the behavioural tests above (trap V3).

        Catches a NEW caller added later that reaches for the tier price table
        again — the behavioural tests only cover the callers that exist today.
        """
        import re
        from pathlib import Path

        src = (Path(__file__).resolve().parent.parent / "app" / "subscription_service.py").read_text()
        for match in re.finditer(r"real_usd\s*=\s*([^\n,]+)", src):
            expr = match.group(1)
            assert "TIER_USD_PRICE" not in expr, (
                f"real_usd is being fed from the tier list-price table: {expr!r}"
            )
