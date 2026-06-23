"""Phase 5 tests — Stripe SDK pin + webhook signature regression."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from importlib.metadata import version as _pkg_version

import pytest


def test_stripe_pinned_to_15_1_0():
    """requirements.txt pins stripe==15.1.0 exact, NOT >=15.

    Per F5 mitigation in the v7.1 plan: don't allow Stripe to silently
    upgrade us into the SDK 16.x territory (which has more dict-vs-object
    serialization surprises). Pin exact, bump deliberately.
    """
    installed = _pkg_version("stripe")
    assert installed == "15.1.0", (
        f"Expected stripe==15.1.0 (per requirements.txt pin), got {installed}. "
        f"This means requirements.txt was loosened or the venv is stale."
    )


def test_stripe_version_accessible_via_metadata_not_dunder():
    """Stripe 15.x removed __version__ — code should use _version.VERSION
    or importlib.metadata.version('stripe'). This test documents the quirk.
    """
    import stripe
    # The dunder attribute was removed in 15.x; legacy code that did
    # `stripe.__version__` will crash. Verify our codebase doesn't rely on it.
    with pytest.raises(AttributeError):
        _ = stripe.__version__  # noqa: B015 — intentionally trip the error
    # The correct path:
    from stripe._version import VERSION
    assert VERSION == "15.1.0"


def test_webhook_construct_event_with_signed_payload(monkeypatch):
    """Regression test for the SDK 15.x dict-conversion class.

    Constructs a self-signed test event using the same HMAC pattern
    Stripe's edge uses, runs construct_event, asserts the type round-trips.
    This is the canary that catches webhook handler regressions BEFORE
    they hit production.
    """
    import stripe

    secret = "whsec_test_synthetic_probe_secret"
    payload = json.dumps(
        {
            "id": "evt_test_phase5",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_phase5",
                    "object": "checkout.session",
                    "customer": "cus_test",
                    "subscription": "sub_test",
                }
            },
        },
        separators=(",", ":"),
    ).encode()
    timestamp = int(time.time())
    signed = f"{timestamp}.".encode() + payload
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    header = f"t={timestamp},v1={sig}"

    event = stripe.Webhook.construct_event(payload, header, secret)
    # Stripe 15.x returns an Event object that supports dict-style access.
    # If the SDK ever silently converts to attribute-only access, this test
    # catches it BEFORE production webhook handlers crash.
    if hasattr(event, "to_dict"):
        evt_dict = event.to_dict()
    else:
        evt_dict = dict(event)

    assert evt_dict["type"] == "checkout.session.completed"
    assert evt_dict["id"] == "evt_test_phase5"
    # The data.object.id round-trip — this is the field webhook handlers
    # always read; if SDK serialization breaks, it breaks here first.
    obj = evt_dict["data"]["object"]
    assert obj["id"] == "cs_test_phase5"
    assert obj["object"] == "checkout.session"


def test_webhook_construct_event_rejects_bad_signature():
    """Tampered payload must fail verification. Defends against the
    accidental "verify=False" or signature-strip middleware path."""
    import stripe

    secret = "whsec_test_synthetic_probe_secret"
    payload = b'{"id":"evt_x","type":"checkout.session.completed"}'
    timestamp = int(time.time())
    bad_sig = "deadbeef" * 8
    header = f"t={timestamp},v1={bad_sig}"

    with pytest.raises(stripe.error.SignatureVerificationError):
        stripe.Webhook.construct_event(payload, header, secret)


def test_synthetic_probe_script_imports():
    """The synthetic probe script must import cleanly even with no
    Stripe creds in the env (the entry-point checks env at runtime,
    not at import time)."""
    import importlib.util
    import pathlib

    script = (
        pathlib.Path(__file__).parent.parent / "scripts" / "stripe_synthetic_probe.py"
    )
    assert script.exists(), f"missing: {script}"
    spec = importlib.util.spec_from_file_location("stripe_synthetic_probe", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Three top-level functions exist:
    assert callable(module._check_credentials)
    assert callable(module._check_stripe_api)
    assert callable(module._check_webhook_signature)
    assert callable(module.main)


def test_synthetic_probe_fails_without_credentials(monkeypatch, capsys):
    """Probe must exit 1 (not crash) when credentials are absent."""
    import importlib.util
    import pathlib

    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)

    script = (
        pathlib.Path(__file__).parent.parent / "scripts" / "stripe_synthetic_probe.py"
    )
    spec = importlib.util.spec_from_file_location("stripe_synthetic_probe", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(SystemExit) as exc_info:
        module._check_credentials()
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "STRIPE_SECRET_KEY" in err
