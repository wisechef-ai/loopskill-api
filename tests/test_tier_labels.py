"""Tests for app/tier_labels.py — SSOT display label helper (RCP-7-A-backend)."""
import importlib
import sys
import pytest


def _reload_tier_labels():
    """Force reload of tier_labels so lru_cache doesn't bleed between tests."""
    for mod in list(sys.modules.keys()):
        if 'tier_labels' in mod:
            del sys.modules[mod]
    import app.tier_labels
    return app.tier_labels


class TestDisplayLabel:
    def test_cook_maps_to_pro(self):
        tl = _reload_tier_labels()
        assert tl.display_label('cook') == 'Pro'

    def test_operator_maps_to_pro_plus(self):
        tl = _reload_tier_labels()
        assert tl.display_label('operator') == 'Pro+'

    def test_free_maps_to_free(self):
        tl = _reload_tier_labels()
        assert tl.display_label('free') == 'Free'

    def test_unknown_slug_falls_back_to_title(self):
        tl = _reload_tier_labels()
        # Unknown slugs fall back to .title()
        result = tl.display_label('unknown_tier')
        assert result == 'Unknown_Tier'

    def test_empty_slug_does_not_raise(self):
        tl = _reload_tier_labels()
        result = tl.display_label('')
        assert isinstance(result, str)


class TestTiersYamlPath:
    def test_tiers_yaml_exists(self):
        from app.tier_labels import TIERS_YAML
        assert TIERS_YAML.exists(), f"tiers.yaml not found at {TIERS_YAML}"

    def test_tiers_yaml_has_cook_and_operator(self):
        from app.tier_labels import _tiers
        tiers = _tiers()
        assert 'cook' in tiers
        assert 'operator' in tiers
        assert tiers['cook']['display_name'] == 'Pro'
        assert tiers['operator']['display_name'] == 'Pro+'


class TestRateLimitUpgradeMessage:
    """Regression: the 429 rate-limit response must use 'Pro+' not 'Operator'."""

    def test_upgrade_message_uses_pro_plus(self):
        """Ensure display_label('operator') returns 'Pro+' so the 429 body is correct."""
        tl = _reload_tier_labels()
        label = tl.display_label('operator')
        # Build the message the same way routes.py does
        install_limit = 100
        caller_tier = 'cook'
        msg = (
            f"Install rate limit exceeded ({install_limit}/day for {caller_tier} tier). "
            f"Upgrade to {label} for unlimited installs."
        )
        assert 'Pro+' in msg
        assert 'Operator' not in msg
