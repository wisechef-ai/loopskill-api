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
"""

from __future__ import annotations

from pathlib import Path

_CONFIG_PY = Path(__file__).resolve().parent.parent / "app" / "config.py"


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
