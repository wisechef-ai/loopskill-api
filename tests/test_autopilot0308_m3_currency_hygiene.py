"""Phase M3 (autopilot_0308) — currency hygiene lock.

D-018 #3 (hub.md): LoopSkill's currency is USD, never EUR. ``app/config.py`` had a
genuine defect (not a typo): the comment documenting the Stripe Pro price env var
said "(€20/mo)" -- wrong currency *and* wrong number. The real Pro price is
``$9.95`` (``config/tiers.yaml`` price_usd, D-004: unchanged).

This locks the fix and prevents a stale EUR price comment from creeping back into
``app/config.py``. It is deliberately scoped to that one file/comment -- other EUR
occurrences in the repo (VAT docstring examples, the payout_engine placeholder
comment) are judgment calls handled separately and are not blanket-banned, since a
EUR example can be legitimately correct in EU-VAT-specific logic.

Also locks three narrower comment-only fixes (no logic/constant change in any of
these -- ``REVENUE_PER_INSTALL_CENTS`` stays 200, D-013: payout engine gets no code
change):
  - ``app/payout_engine.py``: the placeholder-rate comment and the sub-$1-payout
    comment illustrated amounts in EUR; corrected to USD (the actual billing
    currency).
  - ``app/stripe_service.py``: ``create_transfer``'s docstring example and the
    Stripe-minimum comment illustrated amounts in EUR; ``currency`` is a generic
    caller-supplied param, not EU-VAT-specific, so USD is the more honest example.
  - ``app/vat.py``: the ``gross_amount_cents`` docstring example is EU-VAT-*adjacent*
    logic but the amount itself is always in the platform's USD billing currency,
    so the example was corrected -- the EU_VAT_RATES table itself (legitimately
    EUR-country-rate data) is untouched.
"""

from __future__ import annotations

from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent / "app"
_CONFIG_PY = _APP_DIR / "config.py"
_PAYOUT_ENGINE_PY = _APP_DIR / "payout_engine.py"
_STRIPE_SERVICE_PY = _APP_DIR / "stripe_service.py"
_VAT_PY = _APP_DIR / "vat.py"


def test_config_py_stripe_price_comment_has_no_eur():
    """The Stripe price env var comment block must not claim a EUR price."""
    text = _CONFIG_PY.read_text()
    assert "€20/mo" not in text, (
        "app/config.py still documents Pro at the stale, wrong '(€20/mo)'. "
        "The real price is $9.95 USD (config/tiers.yaml, D-004)."
    )
    assert "€100/mo" not in text, (
        "app/config.py still documents Pro+ in EUR. Currency is USD, never EUR (D-018 #3)."
    )
    assert "€" not in text, "app/config.py must not contain a EUR symbol anywhere (D-018 #3)."


def test_config_py_stripe_price_comment_documents_usd_pro_price():
    """The comment must document the real, current Pro price: $9.95/mo USD."""
    text = _CONFIG_PY.read_text()
    assert "$9.95/mo" in text, (
        "app/config.py should document WR_STRIPE_PRICE_PRO as $9.95/mo "
        "(config/tiers.yaml price_usd, D-004: unchanged)."
    )


def test_payout_engine_placeholder_comments_use_usd_not_eur():
    """payout_engine.py's illustrative comments must say USD, not EUR.

    REVENUE_PER_INSTALL_CENTS itself (the value 200) is NOT asserted here --
    D-013 forbids changing payout_engine logic/constants. Only the comment text.
    """
    text = _PAYOUT_ENGINE_PY.read_text()
    assert "€" not in text, (
        "app/payout_engine.py comments must not use EUR symbols (D-018 #3). "
        "The value REVENUE_PER_INSTALL_CENTS = 200 must stay unchanged (D-013)."
    )
    assert "REVENUE_PER_INSTALL_CENTS = 200" in text, (
        "D-013: payout_engine.py must not have its logic/constants changed, only comments."
    )


def test_stripe_service_transfer_docstring_uses_usd_not_eur():
    """stripe_service.py's create_transfer docstring/comment must say USD, not EUR."""
    text = _STRIPE_SERVICE_PY.read_text()
    assert "€" not in text, "app/stripe_service.py must not use EUR symbols (D-018 #3)."


def test_vat_py_docstring_example_uses_usd_not_eur():
    """vat.py's gross_amount_cents docstring example must say USD, not EUR.

    EU_VAT_RATES itself (legitimate EU-country VAT-rate data) is untouched --
    only the illustrative billing-currency example in the docstring.
    """
    text = _VAT_PY.read_text()
    assert "e.g., 4999 = €49.99" not in text, (
        "app/vat.py's gross_amount_cents docstring example should illustrate the "
        "platform's actual billing currency (USD), not EUR."
    )
    assert "$49.99" in text, "app/vat.py's docstring example should use a USD amount."
