"""Tests for TIER_PRICE_IDS and TIER_USD_PRICE loading from config/tiers.yaml.

Verifies RCP-INCIDENT-2026-05-11 Phase 3 requirement:
- TIER_PRICE_IDS contains 'cook' and 'operator', not 'studio'
- TIER_USD_PRICE is correctly loaded from yaml
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

    def test_tier_price_ids_contains_operator_not_studio(self):
        """TIER_PRICE_IDS must have 'operator' key, not 'studio'."""
        ss = self._fresh_ss()
        assert "operator" in ss.TIER_PRICE_IDS, (
            f"Expected 'operator' in TIER_PRICE_IDS, got keys: {list(ss.TIER_PRICE_IDS)}"
        )
        assert "studio" not in ss.TIER_PRICE_IDS, (
            "'studio' key must not appear in TIER_PRICE_IDS after Phase 3 migration"
        )

    def test_tier_price_ids_contains_cook(self):
        """TIER_PRICE_IDS must still have 'cook'."""
        ss = self._fresh_ss()
        assert "cook" in ss.TIER_PRICE_IDS

    def test_tier_price_ids_does_not_contain_free(self):
        """Free tier has no Stripe price — must be absent from TIER_PRICE_IDS."""
        ss = self._fresh_ss()
        assert "free" not in ss.TIER_PRICE_IDS

    def test_tier_usd_price_loaded_from_yaml(self):
        """TIER_USD_PRICE values come from config/tiers.yaml price_usd fields."""
        from pathlib import Path
        import yaml
        yaml_path = Path(__file__).parent.parent / "config" / "tiers.yaml"
        with open(yaml_path) as f:
            tiers = yaml.safe_load(f)["tiers"]

        ss = self._fresh_ss()

        # cook: 20, operator: 100 per tiers.yaml
        assert ss.TIER_USD_PRICE.get("cook") == float(tiers["cook"]["price_usd"])
        assert ss.TIER_USD_PRICE.get("operator") == float(tiers["operator"]["price_usd"])

    def test_tier_usd_price_has_legacy_studio_alias(self):
        """TIER_USD_PRICE retains 'studio' key as 30-day backwards-compat shim."""
        ss = self._fresh_ss()
        # studio should map to the same price as operator (100.0)
        assert "studio" in ss.TIER_USD_PRICE
        assert ss.TIER_USD_PRICE["studio"] == ss.TIER_USD_PRICE["operator"]

    def test_load_tier_price_ids_helper_returns_correct_slugs(self):
        """_load_tier_price_ids() returns dict with 'cook' and 'operator' keys."""
        ss = self._fresh_ss()
        ss._load_tier_price_ids.cache_clear()
        result = ss._load_tier_price_ids()
        assert "cook" in result
        assert "operator" in result
        assert "studio" not in result
        assert "free" not in result

    def test_load_tier_usd_price_helper(self):
        """_load_tier_usd_price() returns float prices from yaml."""
        ss = self._fresh_ss()
        ss._load_tier_usd_price.cache_clear()
        result = ss._load_tier_usd_price()
        assert result["cook"] == 20.0
        assert result["operator"] == 100.0
        assert result["free"] == 0.0
