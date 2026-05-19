"""Root-level conftest.py — runs before ANY test module is imported.

Sets WR_DATABASE_URL to sqlite so that:
  - the global `settings = Settings()` in app/config.py does not trigger the
    production-secrets RuntimeError (secfix_1905 Issue #1 gate)
  - the in-memory SQLite test engine in tests/conftest.py continues to work

This must live at the repo root (not inside tests/) so it is executed BEFORE
pytest begins collecting or importing test modules.
"""
import os

# Must be set before any app.* import so Settings() picks it up.
os.environ.setdefault("WR_DATABASE_URL", "sqlite:///./test_dev.db")
# COOKIES_SECURE defaults to True; in sqlite test env we allow False.
os.environ.setdefault("WR_COOKIES_SECURE", "false")

# Stripe price IDs for the test environment. config/tiers.yaml maps the
# `pro` / `pro_plus` tiers to WR_STRIPE_PRICE_PRO / WR_STRIPE_PRICE_PRO_PLUS;
# subscription_service._load_tier_price_ids() reads these at import time and
# builds TIER_PRICE_IDS. Without them TIER_PRICE_IDS is empty {}, and every
# checkout / tier test fails with `invalid_tier:... Valid: []`.
# These are dummy IDs — no test ever calls the real Stripe API (all Stripe
# calls are patched). Tests that exercise the canonical/legacy env-var
# fallback (TestEnvVarRenameLegacyFallback) override settings directly via
# _reload_with_settings, so they remain independent of these defaults.
os.environ.setdefault("WR_STRIPE_PRICE_PRO", "price_test_pro")
os.environ.setdefault("WR_STRIPE_PRICE_PRO_PLUS", "price_test_pro_plus")
