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
