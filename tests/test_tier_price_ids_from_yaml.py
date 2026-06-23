"""Tests for TIER_PRICE_IDS and TIER_USD_PRICE loading from config/tiers.yaml.

Verifies RCP-INCIDENT-2026-05-11 Phase 5 requirement:
- TIER_PRICE_IDS contains 'pro' and 'pro_plus', not 'cook', 'operator', or 'studio'
- TIER_USD_PRICE is correctly loaded from yaml with legacy aliases retained
"""
from __future__ import annotations

import importlib


class TestTierPriceIdsFromYaml:
    def _fresh_ss(self):
        """Force-reload subscription_service to bypass lru_cache."""
        from app import subscription_service as ss
        # Bust the lru_cache on the loader helpers
        ss._load_tiers_yaml.cache_clear()
        ss._load_tier_price_ids.cache_clear()
        ss._load_tier_usd_price.cache_clear()
        ss = importlib.reload(ss)
        return ss

    def test_tier_price_ids_contains_pro_and_pro_plus(self):
        """TIER_PRICE_IDS must have 'pro' and 'pro_plus' keys (Phase 5 canonical slugs)."""
        ss = self._fresh_ss()
        assert "pro" in ss.TIER_PRICE_IDS, (
            f"Expected 'pro' in TIER_PRICE_IDS, got keys: {list(ss.TIER_PRICE_IDS)}"
        )
        assert "pro_plus" in ss.TIER_PRICE_IDS, (
            f"Expected 'pro_plus' in TIER_PRICE_IDS, got keys: {list(ss.TIER_PRICE_IDS)}"
        )

    def test_tier_price_ids_does_not_contain_legacy_slugs(self):
        """TIER_PRICE_IDS must NOT have 'cook', 'operator', or 'studio' keys after Phase 5."""
        ss = self._fresh_ss()
        assert "cook" not in ss.TIER_PRICE_IDS, (
            "'cook' key must not appear in TIER_PRICE_IDS after Phase 5 migration"
        )
        assert "operator" not in ss.TIER_PRICE_IDS, (
            "'operator' key must not appear in TIER_PRICE_IDS after Phase 5 migration"
        )
        assert "studio" not in ss.TIER_PRICE_IDS, (
            "'studio' key must not appear in TIER_PRICE_IDS after Phase 5 migration"
        )

    def test_tier_price_ids_does_not_contain_free(self):
        """Free tier has no Stripe price — must be absent from TIER_PRICE_IDS."""
        ss = self._fresh_ss()
        assert "free" not in ss.TIER_PRICE_IDS

    def test_tier_price_ids_exact_keys(self):
        """TIER_PRICE_IDS must have exactly {'pro', 'pro_plus'}."""
        ss = self._fresh_ss()
        assert set(ss.TIER_PRICE_IDS) == {"pro", "pro_plus"}, (
            f"Expected exactly {{pro, pro_plus}}, got: {set(ss.TIER_PRICE_IDS)}"
        )

    def test_tier_usd_price_loaded_from_yaml(self):
        """TIER_USD_PRICE values come from config/tiers.yaml price_usd fields."""
        from pathlib import Path
        import yaml
        yaml_path = Path(__file__).parent.parent / "config" / "tiers.yaml"
        with open(yaml_path) as f:
            tiers = yaml.safe_load(f)["tiers"]

        ss = self._fresh_ss()

        # pro: 20, pro_plus: 100 per tiers.yaml
        assert ss.TIER_USD_PRICE.get("pro") == float(tiers["pro"]["price_usd"])
        assert ss.TIER_USD_PRICE.get("pro_plus") == float(tiers["pro_plus"]["price_usd"])

    def test_tier_usd_price_has_legacy_aliases(self):
        """TIER_USD_PRICE retains legacy slugs as 30-day backwards-compat shim."""
        ss = self._fresh_ss()
        # cook → pro price
        assert "cook" in ss.TIER_USD_PRICE
        assert ss.TIER_USD_PRICE["cook"] == ss.TIER_USD_PRICE["pro"]
        # operator → pro_plus price
        assert "operator" in ss.TIER_USD_PRICE
        assert ss.TIER_USD_PRICE["operator"] == ss.TIER_USD_PRICE["pro_plus"]
        # studio → pro_plus price
        assert "studio" in ss.TIER_USD_PRICE
        assert ss.TIER_USD_PRICE["studio"] == ss.TIER_USD_PRICE["pro_plus"]

    def test_tier_usd_price_values_correct(self):
        """TIER_USD_PRICE canonical values are 20.0 and 100.0."""
        ss = self._fresh_ss()
        assert ss.TIER_USD_PRICE.get("pro") == 20.0
        assert ss.TIER_USD_PRICE.get("pro_plus") == 100.0
        assert ss.TIER_USD_PRICE.get("free") == 0.0

    def test_load_tier_price_ids_helper_returns_correct_slugs(self):
        """_load_tier_price_ids() returns dict with 'pro' and 'pro_plus' keys."""
        ss = self._fresh_ss()
        ss._load_tier_price_ids.cache_clear()
        result = ss._load_tier_price_ids()
        assert "pro" in result
        assert "pro_plus" in result
        assert "cook" not in result
        assert "operator" not in result
        assert "studio" not in result
        assert "free" not in result

    def test_load_tier_usd_price_helper(self):
        """_load_tier_usd_price() returns float prices from yaml."""
        ss = self._fresh_ss()
        ss._load_tier_usd_price.cache_clear()
        result = ss._load_tier_usd_price()
        assert result["pro"] == 20.0
        assert result["pro_plus"] == 100.0
        assert result["free"] == 0.0


class TestEnvVarRenameLegacyFallback:
    """RCP-INCIDENT-2026-05-11 Phase 6 — env var rename soak window.

    The canonical env vars are WR_STRIPE_PRICE_PRO and WR_STRIPE_PRICE_PRO_PLUS.
    Legacy env vars WR_STRIPE_PRICE_COOK / WR_STRIPE_PRICE_OPERATOR /
    WR_STRIPE_PRICE_STUDIO are accepted as fallback until 2026-06-10.
    """

    def _reload_with_settings(self, **overrides):
        """Reload subscription_service with specific settings field overrides."""
        from app import subscription_service as ss
        from app.config import settings
        # Apply overrides
        original = {}
        for k, v in overrides.items():
            original[k] = getattr(settings, k, None)
            setattr(settings, k, v)
        # Bust caches
        ss._load_tiers_yaml.cache_clear()
        ss._load_tier_price_ids.cache_clear()
        ss._load_tier_usd_price.cache_clear()
        return ss, original

    def _restore(self, original):
        from app.config import settings
        for k, v in original.items():
            setattr(settings, k, v)

    def test_canonical_env_var_wins_when_set(self):
        """When STRIPE_PRICE_PRO is set, it's used (not legacy STRIPE_PRICE_COOK)."""
        ss, original = self._reload_with_settings(
            STRIPE_PRICE_PRO="price_CANONICAL_pro",
            STRIPE_PRICE_PRO_PLUS="price_CANONICAL_proplus",
            STRIPE_PRICE_COOK="price_LEGACY_cook",
            STRIPE_PRICE_STUDIO="price_LEGACY_studio",
        )
        try:
            result = ss._load_tier_price_ids()
            assert result["pro"] == "price_CANONICAL_pro", (
                f"Expected canonical, got {result['pro']!r}"
            )
            assert result["pro_plus"] == "price_CANONICAL_proplus"
        finally:
            self._restore(original)

    def test_legacy_env_var_used_when_canonical_empty(self):
        """If WR_STRIPE_PRICE_PRO is empty, fall back to WR_STRIPE_PRICE_COOK."""
        ss, original = self._reload_with_settings(
            STRIPE_PRICE_PRO="",
            STRIPE_PRICE_PRO_PLUS="",
            STRIPE_PRICE_COOK="price_LEGACY_cook_used",
            STRIPE_PRICE_STUDIO="price_LEGACY_studio_used",
        )
        try:
            result = ss._load_tier_price_ids()
            assert result["pro"] == "price_LEGACY_cook_used"
            assert result["pro_plus"] == "price_LEGACY_studio_used"
        finally:
            self._restore(original)

    def test_both_canonical_and_legacy_empty_excludes_tier(self):
        """If neither env var is set, the tier is excluded from TIER_PRICE_IDS."""
        ss, original = self._reload_with_settings(
            STRIPE_PRICE_PRO="",
            STRIPE_PRICE_PRO_PLUS="",
            STRIPE_PRICE_COOK="",
            STRIPE_PRICE_STUDIO="",
        )
        try:
            result = ss._load_tier_price_ids()
            assert "pro" not in result
            assert "pro_plus" not in result
        finally:
            self._restore(original)

    def test_prod_scenario_default_canonical_with_real_legacy_in_env(self):
        """REGRESSION: prod has WR_STRIPE_PRICE_COOK set to the real price ID,
        WR_STRIPE_PRICE_PRO unset. The defaults for STRIPE_PRICE_PRO must NOT
        mask the legacy fallback. This was the bug caught immediately after
        Phase 6 deploy on 2026-05-11."""
        # STRIPE_PRICE_PRO default in config.py is "" (intentionally).
        # STRIPE_PRICE_COOK default in config.py is a stale test ID.
        # The .env on prod overrides STRIPE_PRICE_COOK with the real price ID.
        # The resolver MUST see STRIPE_PRICE_PRO="" and fall back to STRIPE_PRICE_COOK.
        ss, original = self._reload_with_settings(
            STRIPE_PRICE_PRO="",  # canonical default — simulates unset .env
            STRIPE_PRICE_PRO_PLUS="",
            STRIPE_PRICE_COOK="price_PROD_REAL_cook_value",
            STRIPE_PRICE_STUDIO="price_PROD_REAL_studio_value",
        )
        try:
            result = ss._load_tier_price_ids()
            assert result["pro"] == "price_PROD_REAL_cook_value", (
                f"Phase 6 regression: canonical default masked legacy. Got {result['pro']!r}"
            )
            assert result["pro_plus"] == "price_PROD_REAL_studio_value"
        finally:
            self._restore(original)

    def test_canonical_field_default_is_empty(self):
        """RCP-INCIDENT-2026-05-11 Phase 6 hotfix: STRIPE_PRICE_PRO and
        STRIPE_PRICE_PRO_PLUS MUST default to '' so an unset .env triggers
        the legacy-env-var fallback. Any non-empty default would mask the
        real prod value sourced via WR_STRIPE_PRICE_COOK/STUDIO."""
        from app.config import Settings
        defaults = Settings.model_fields
        assert defaults["STRIPE_PRICE_PRO"].default == "", (
            "STRIPE_PRICE_PRO must default to '' so unset .env falls back to legacy. "
            f"Got {defaults['STRIPE_PRICE_PRO'].default!r}"
        )
        assert defaults["STRIPE_PRICE_PRO_PLUS"].default == "", (
            "STRIPE_PRICE_PRO_PLUS must default to '' so unset .env falls back to legacy. "
            f"Got {defaults['STRIPE_PRICE_PRO_PLUS'].default!r}"
        )
