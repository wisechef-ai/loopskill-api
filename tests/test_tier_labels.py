"""Tests for app/tier_labels.py — SSOT display label helper (Phase 5 parity update)."""
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
    def test_pro_maps_to_pro(self):
        tl = _reload_tier_labels()
        assert tl.display_label('pro') == 'Pro'

    def test_pro_plus_maps_to_pro_plus(self):
        tl = _reload_tier_labels()
        assert tl.display_label('pro_plus') == 'Pro+'

    def test_cook_maps_to_pro_via_legacy_shim(self):
        """Legacy 'cook' slug maps to 'Pro' via _LEGACY_SLUG_MAP."""
        tl = _reload_tier_labels()
        assert tl.display_label('cook') == 'Pro'

    def test_operator_maps_to_pro_plus_via_legacy_shim(self):
        """Legacy 'operator' slug maps to 'Pro+' via _LEGACY_SLUG_MAP."""
        tl = _reload_tier_labels()
        assert tl.display_label('operator') == 'Pro+'

    def test_studio_maps_to_pro_plus_via_legacy_shim(self):
        """Legacy 'studio' slug maps to 'Pro+' via _LEGACY_SLUG_MAP."""
        tl = _reload_tier_labels()
        assert tl.display_label('studio') == 'Pro+'

    def test_free_maps_to_free(self):
        tl = _reload_tier_labels()
        assert tl.display_label('free') == 'Free'

    def test_unknown_slug_falls_back_to_title(self):
        tl = _reload_tier_labels()
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

    def test_tiers_yaml_has_pro_and_pro_plus(self):
        """Phase 5: yaml keys are now 'pro' and 'pro_plus'."""
        from app.tier_labels import _tiers
        tiers = _tiers()
        assert 'pro' in tiers, f"'pro' key missing from tiers.yaml, got: {list(tiers.keys())}"
        assert 'pro_plus' in tiers, f"'pro_plus' key missing from tiers.yaml, got: {list(tiers.keys())}"
        assert tiers['pro']['display_name'] == 'Pro'
        assert tiers['pro_plus']['display_name'] == 'Pro+'

    def test_tiers_yaml_has_no_cook_or_operator_keys(self):
        """Phase 5: 'cook' and 'operator' are no longer yaml keys."""
        from app.tier_labels import _tiers
        tiers = _tiers()
        assert 'cook' not in tiers, "'cook' key must not appear in tiers.yaml after Phase 5"
        assert 'operator' not in tiers, "'operator' key must not appear in tiers.yaml after Phase 5"


class TestIsPaidTier:
    def test_pro_is_paid(self):
        tl = _reload_tier_labels()
        assert tl._is_paid_tier('pro') is True

    def test_pro_plus_is_paid(self):
        tl = _reload_tier_labels()
        assert tl._is_paid_tier('pro_plus') is True

    def test_legacy_cook_is_paid(self):
        tl = _reload_tier_labels()
        assert tl._is_paid_tier('cook') is True

    def test_legacy_operator_is_paid(self):
        tl = _reload_tier_labels()
        assert tl._is_paid_tier('operator') is True

    def test_legacy_studio_is_paid(self):
        tl = _reload_tier_labels()
        assert tl._is_paid_tier('studio') is True

    def test_free_not_paid(self):
        tl = _reload_tier_labels()
        assert tl._is_paid_tier('free') is False

    def test_none_not_paid(self):
        tl = _reload_tier_labels()
        assert tl._is_paid_tier(None) is False


class TestIsProPlusTier:
    def test_pro_plus_is_pro_plus(self):
        tl = _reload_tier_labels()
        assert tl._is_pro_plus_tier('pro_plus') is True

    def test_legacy_operator_is_pro_plus(self):
        tl = _reload_tier_labels()
        assert tl._is_pro_plus_tier('operator') is True

    def test_legacy_studio_is_pro_plus(self):
        tl = _reload_tier_labels()
        assert tl._is_pro_plus_tier('studio') is True

    def test_cook_is_not_pro_plus(self):
        tl = _reload_tier_labels()
        assert tl._is_pro_plus_tier('cook') is False

    def test_pro_is_not_pro_plus(self):
        tl = _reload_tier_labels()
        assert tl._is_pro_plus_tier('pro') is False

    def test_none_is_not_pro_plus(self):
        tl = _reload_tier_labels()
        assert tl._is_pro_plus_tier(None) is False

    def test_free_is_not_pro_plus(self):
        tl = _reload_tier_labels()
        assert tl._is_pro_plus_tier('free') is False


class TestIsOperatorTierWrapper:
    """Verify _is_operator_tier still delegates correctly to _is_pro_plus_tier."""

    def test_pro_plus_returns_true(self):
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier('pro_plus') is True

    def test_operator_returns_true(self):
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier('operator') is True

    def test_studio_returns_true(self):
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier('studio') is True

    def test_pro_returns_false(self):
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier('pro') is False

    def test_cook_returns_false(self):
        from app.tier_labels import _is_operator_tier
        assert _is_operator_tier('cook') is False


class TestRateLimitUpgradeMessage:
    """Regression: the 429 rate-limit response must use 'Pro+' not 'Operator'."""

    def test_upgrade_message_uses_pro_plus(self):
        """Ensure display_label('pro_plus') returns 'Pro+' so the 429 body is correct."""
        tl = _reload_tier_labels()
        label = tl.display_label('pro_plus')
        install_limit = 100
        caller_tier = 'pro'
        msg = (
            f"Install rate limit exceeded ({install_limit}/day for {caller_tier} tier). "
            f"Upgrade to {label} for unlimited installs."
        )
        assert 'Pro+' in msg
        assert 'Operator' not in msg


class TestCookbookLimitSSOT:
    """loopclose_3005 Phase A — cookbook_limit() reads config/tiers.yaml SSOT."""

    def test_free_is_zero(self):
        tl = _reload_tier_labels()
        assert tl.cookbook_limit("free") == 0

    def test_pro_is_ten(self):
        tl = _reload_tier_labels()
        assert tl.cookbook_limit("pro") == 10

    def test_pro_plus_is_two_hundred(self):
        tl = _reload_tier_labels()
        assert tl.cookbook_limit("pro_plus") == 200

    def test_legacy_cook_resolves_to_pro_ten(self):
        tl = _reload_tier_labels()
        assert tl.cookbook_limit("cook") == 10

    def test_legacy_operator_resolves_to_pro_plus_two_hundred(self):
        tl = _reload_tier_labels()
        assert tl.cookbook_limit("operator") == 200

    def test_legacy_studio_resolves_to_pro_plus_two_hundred(self):
        tl = _reload_tier_labels()
        assert tl.cookbook_limit("studio") == 200

    def test_none_falls_back_to_free_zero(self):
        tl = _reload_tier_labels()
        assert tl.cookbook_limit(None) == 0

    def test_unknown_tier_falls_back_to_free_zero(self):
        tl = _reload_tier_labels()
        assert tl.cookbook_limit("enterprise_made_up") == 0
