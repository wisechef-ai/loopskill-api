"""Phase 4 — boot-time Stripe webhook endpoint smoke test.

Tests for ``verify_stripe_webhook_endpoint()`` in app/startup_checks.py.

Imports the function directly from startup_checks to avoid triggering
create_app() (which connects to postgres) when importing app.main.

Test cases:
1. Exactly one enabled endpoint at expected URL → no alert, no CRITICAL log.
2. Zero endpoints → CRITICAL log + alert call made.
3. Duplicate (>1) endpoints → CRITICAL log + alert call.
4. Stripe call raises → fail-soft, service continues, only WARNING logged.
5. WR_STRIPE_WEBHOOK_SECRET doesn't start with whsec_ → CRITICAL + alert.
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest


# The expected production URL (must match the constant in app/startup_checks.py)
_EXPECTED_WEBHOOK_URL = "https://recipes.wisechef.ai/api/stripe/webhook"


def _make_endpoint(url: str = _EXPECTED_WEBHOOK_URL, status: str = "enabled") -> dict:
    return {
        "id": "we_test_abc",
        "url": url,
        "status": status,
    }


def _run(coro):
    """Run a coroutine synchronously using the default event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def non_sqlite_db(monkeypatch):
    """Pretend DATABASE_URL is postgres so the sqlite skip guard doesn't fire."""
    from app.config import settings
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql://test@localhost/testdb")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_dummy")


# ── Test 1: exactly one enabled endpoint → no alert, no CRITICAL ──────────────

def test_one_enabled_endpoint_no_alert(caplog):
    """Gate 1: healthy config — exactly one enabled endpoint → no alert."""
    from app.startup_checks import verify_stripe_webhook_endpoint

    endpoints_resp = {"data": [_make_endpoint()]}

    with patch("stripe.WebhookEndpoint.list", return_value=endpoints_resp), \
         patch.dict("os.environ", {
             "WR_STRIPE_WEBHOOK_SECRET": "whsec_valid_secret",
             "TORI_DISCORD_WEBHOOK_URL": "",
         }), \
         patch("app.startup_checks.post_tori_alert") as mock_alert, \
         caplog.at_level(logging.CRITICAL, logger="app.startup_checks"):
        _run(verify_stripe_webhook_endpoint())

    mock_alert.assert_not_called()
    # No CRITICAL records in log
    critical_records = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert not critical_records, f"Unexpected CRITICAL logs: {[r.message for r in critical_records]}"


# ── Test 2: zero endpoints → CRITICAL + alert ──────────────────────────────────

def test_zero_endpoints_critical_and_alert(caplog):
    """Gate 2: no enabled endpoints → CRITICAL log + Discord alert."""
    from app.startup_checks import verify_stripe_webhook_endpoint

    endpoints_resp = {"data": []}  # empty

    with patch("stripe.WebhookEndpoint.list", return_value=endpoints_resp), \
         patch.dict("os.environ", {
             "WR_STRIPE_WEBHOOK_SECRET": "whsec_valid_secret",
             "TORI_DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
         }), \
         patch("app.startup_checks.post_tori_alert") as mock_alert, \
         caplog.at_level(logging.CRITICAL, logger="app.startup_checks"):
        _run(verify_stripe_webhook_endpoint())

    mock_alert.assert_called_once()
    alert_msg = mock_alert.call_args[0][0]
    assert "CRITICAL" in alert_msg or "No enabled" in alert_msg

    critical_records = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert critical_records, "Expected at least one CRITICAL log record"
    assert any(
        "zero" in r.message.lower() or "enabled" in r.message.lower()
        for r in critical_records
    )


# ── Test 3: duplicate endpoints → CRITICAL + alert ───────────────────────────

def test_duplicate_endpoints_critical_and_alert(caplog):
    """Gate 3: two enabled endpoints found → CRITICAL log + Discord alert."""
    from app.startup_checks import verify_stripe_webhook_endpoint

    endpoints_resp = {"data": [
        _make_endpoint(),
        _make_endpoint(url=_EXPECTED_WEBHOOK_URL, status="enabled"),
    ]}

    with patch("stripe.WebhookEndpoint.list", return_value=endpoints_resp), \
         patch.dict("os.environ", {
             "WR_STRIPE_WEBHOOK_SECRET": "whsec_valid_secret",
             "TORI_DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
         }), \
         patch("app.startup_checks.post_tori_alert") as mock_alert, \
         caplog.at_level(logging.CRITICAL, logger="app.startup_checks"):
        _run(verify_stripe_webhook_endpoint())

    mock_alert.assert_called_once()
    alert_msg = mock_alert.call_args[0][0]
    assert "CRITICAL" in alert_msg or "2" in alert_msg or "Duplicate" in alert_msg

    critical_records = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert critical_records, "Expected at least one CRITICAL log record"
    assert any(
        "2" in r.message or "expected 1" in r.message.lower()
        for r in critical_records
    )


# ── Test 4: Stripe call raises → fail-soft, warning only ─────────────────────

def test_stripe_raises_fail_soft(caplog):
    """Gate 4: stripe.WebhookEndpoint.list raises → warning logged, no crash."""
    from app.startup_checks import verify_stripe_webhook_endpoint

    with patch("stripe.WebhookEndpoint.list", side_effect=Exception("Stripe timeout")), \
         patch.dict("os.environ", {
             "WR_STRIPE_WEBHOOK_SECRET": "whsec_valid_secret",
             "TORI_DISCORD_WEBHOOK_URL": "",
         }), \
         patch("app.startup_checks.post_tori_alert") as mock_alert, \
         caplog.at_level(logging.WARNING, logger="app.startup_checks"):
        # Must not raise
        _run(verify_stripe_webhook_endpoint())

    # No alert should fire (the exception is caught at the outer guard)
    mock_alert.assert_not_called()

    # At least a WARNING log
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "Expected at least one WARNING log when Stripe raises"


# ── Test 5: invalid webhook secret format → CRITICAL + alert ──────────────────

def test_invalid_webhook_secret_critical_and_alert(caplog):
    """Gate 5: WR_STRIPE_WEBHOOK_SECRET doesn't start with whsec_ → CRITICAL + alert."""
    from app.startup_checks import verify_stripe_webhook_endpoint

    endpoints_resp = {"data": [_make_endpoint()]}

    with patch("stripe.WebhookEndpoint.list", return_value=endpoints_resp), \
         patch.dict("os.environ", {
             "WR_STRIPE_WEBHOOK_SECRET": "sk_wrong_format",
             "TORI_DISCORD_WEBHOOK_URL": "https://discord.example/webhook",
         }), \
         patch("app.startup_checks.post_tori_alert") as mock_alert, \
         caplog.at_level(logging.CRITICAL, logger="app.startup_checks"):
        _run(verify_stripe_webhook_endpoint())

    mock_alert.assert_called()
    critical_records = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert critical_records, "Expected CRITICAL log for bad secret format"
    assert any(
        "whsec_" in r.message or "invalid" in r.message.lower()
        for r in critical_records
    )
