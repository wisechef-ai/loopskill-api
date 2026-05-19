"""Issue #23 — Stripe price defaults emptied; boot refuses when both canonical
and legacy price IDs are empty in non-sqlite envs.

Tests:
1. Legacy price IDs (COOK / OPERATOR / STUDIO) default to "" in Settings.
2. Canonical price IDs (PRO / PRO_PLUS) still default to "".
3. _assert_production_secrets raises when both canonical and legacy are empty.
4. sqlite env tolerates empty price IDs (dev env exempt).
5. Setting only the legacy alias satisfies the check.
"""

from __future__ import annotations

import os
from unittest.mock import patch


def _make_settings_non_sqlite(**overrides):
    """Build a minimal Settings-like object for testing _assert_production_secrets."""
    from app.config import Settings

    # Use a non-sqlite URL to trigger production checks.
    # We must also provide the required secrets to avoid the secrets gate.
    env = {
        "WR_DATABASE_URL": "postgresql://wisechef@localhost/wiserecipes_test",
        "WR_API_KEY": "rec_prod_test_key_1234567890abcdef",
        "WR_SIGNING_SECRET": "wr-tarball-signing-prod-safe-value",
        "WR_JWT_SECRET": "wr-jwt-prod-safe-value-12345678",
        "WR_HEARTBEAT_PEPPER": "wr-fleet-pepper-prod-safe-value",
        "WR_OAUTH_REDIRECT_BASE": "https://recipes.wisechef.ai",
        "WR_COOKIES_SECURE": "true",
        **overrides,
    }
    with patch.dict(os.environ, env, clear=False):
        return Settings()


# ── 1. Legacy defaults are now "" ─────────────────────────────────────────────

def test_legacy_price_defaults_empty() -> None:
    """STRIPE_PRICE_COOK / OPERATOR / STUDIO must default to empty string."""
    from app.config import Settings
    # Use sqlite so no validation runs
    with patch.dict(os.environ, {"WR_DATABASE_URL": "sqlite:///dev.db"}, clear=False):
        s = Settings()
    assert s.STRIPE_PRICE_COOK == ""
    assert s.STRIPE_PRICE_OPERATOR == ""
    assert s.STRIPE_PRICE_STUDIO == ""


# ── 2. Canonical defaults remain "" ───────────────────────────────────────────

def test_canonical_price_defaults_empty() -> None:
    """STRIPE_PRICE_PRO and STRIPE_PRICE_PRO_PLUS must default to empty string.

    This asserts the Settings *field default*, so it must isolate from the
    ambient environment — the test conftest sets WR_STRIPE_PRICE_PRO /
    WR_STRIPE_PRICE_PRO_PLUS so TIER_PRICE_IDS is populated for the checkout
    tests, and patch.dict(clear=False) would otherwise let those leak in.
    We explicitly drop them so this test sees the true default.
    """
    from app.config import Settings
    env = {k: v for k, v in os.environ.items()
           if k not in ("WR_STRIPE_PRICE_PRO", "WR_STRIPE_PRICE_PRO_PLUS")}
    env["WR_DATABASE_URL"] = "sqlite:///dev.db"
    with patch.dict(os.environ, env, clear=True):
        s = Settings()
    assert s.STRIPE_PRICE_PRO == ""
    assert s.STRIPE_PRICE_PRO_PLUS == ""


# ── 3. Non-sqlite + both empty → raises ───────────────────────────────────────

def test_empty_price_ids_raise_in_prod() -> None:
    """Raising RuntimeError when both canonical and legacy price IDs are empty."""
    import pytest

    env_overrides = {
        "WR_STRIPE_PRICE_PRO": "",
        "WR_STRIPE_PRICE_PRO_PLUS": "",
        "WR_STRIPE_PRICE_COOK": "",
        "WR_STRIPE_PRICE_OPERATOR": "",
        "WR_STRIPE_PRICE_STUDIO": "",
    }
    with pytest.raises(RuntimeError, match="Stripe price IDs are empty"):
        _make_settings_non_sqlite(**env_overrides)


# ── 4. sqlite env tolerates empty ─────────────────────────────────────────────

def test_sqlite_env_tolerates_empty_prices() -> None:
    """sqlite DB URL bypasses the price-ID gate (dev env)."""
    from app.config import Settings
    env = {
        "WR_DATABASE_URL": "sqlite:///dev.db",
        "WR_STRIPE_PRICE_PRO": "",
        "WR_STRIPE_PRICE_COOK": "",
    }
    with patch.dict(os.environ, env, clear=False):
        s = Settings()  # must not raise
    assert s.STRIPE_PRICE_PRO == ""


# ── 5. Legacy alias is sufficient ─────────────────────────────────────────────

def test_legacy_alias_satisfies_price_check() -> None:
    """Setting STRIPE_PRICE_COOK (legacy) alone passes the gate."""
    env_overrides = {
        "WR_STRIPE_PRICE_PRO": "",
        "WR_STRIPE_PRICE_PRO_PLUS": "",
        "WR_STRIPE_PRICE_COOK": "price_legacy_cook_id",
        "WR_STRIPE_PRICE_OPERATOR": "price_legacy_operator_id",
        "WR_STRIPE_PRICE_STUDIO": "",
    }
    s = _make_settings_non_sqlite(**env_overrides)  # must not raise
    assert s.STRIPE_PRICE_COOK == "price_legacy_cook_id"
