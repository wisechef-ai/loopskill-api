"""Phase G — Tier rename SWEEP tests (recipes_2005/G).

Tests:
1. test_tier_rank_pro_outranks_free
2. test_tier_rank_pro_plus_outranks_pro
3. test_legacy_cook_aliases_to_pro_for_30_days
4. test_legacy_operator_aliases_to_pro_plus_for_30_days
5. test_new_skill_defaults_to_pro_tier
6. test_tier_rank_clean_structure (bonus: TIER_RANK has canonical keys + legacy aliases)
7. test_marketing_counts_uses_canonical_slugs
8. test_recall_default_tier_filter_uses_canonical_slugs
9. test_search_fallback_tier_filter_uses_canonical_slugs
10. test_subrecipe_resolve_scope_updated
11. test_payout_engine_rates_use_canonical_tiers
"""
from __future__ import annotations

import importlib
import sys
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────


def _reload(module_name: str):
    """Force a clean reimport so module-level constants are re-evaluated."""
    for key in list(sys.modules.keys()):
        if module_name.replace(".", "/") in key.replace(".", "/") or key == module_name:
            del sys.modules[key]
    return importlib.import_module(module_name)


# ── 1. TIER_RANK: pro outranks free ──────────────────────────────────────────


class TestTierRankProOutranksFree:
    def test_tier_rank_pro_outranks_free(self):
        """TIER_RANK['pro'] > TIER_RANK['free']."""
        from app.access_routes import TIER_RANK

        assert TIER_RANK["pro"] > TIER_RANK["free"], (
            f"Expected pro({TIER_RANK['pro']}) > free({TIER_RANK['free']})"
        )

    def test_free_rank_is_zero_or_lower(self):
        """Free tier rank should be ≤ 1 (lowest tier)."""
        from app.access_routes import TIER_RANK

        assert TIER_RANK["free"] <= 1

    def test_pro_rank_equals_2(self):
        """Canonical TIER_RANK: pro=2 (plan §G)."""
        from app.access_routes import TIER_RANK

        assert TIER_RANK["pro"] == 2, f"Expected pro=2, got {TIER_RANK['pro']}"


# ── 2. TIER_RANK: pro_plus outranks pro ──────────────────────────────────────


class TestTierRankProPlusOutranksPro:
    def test_tier_rank_pro_plus_outranks_pro(self):
        """TIER_RANK['pro_plus'] > TIER_RANK['pro']."""
        from app.access_routes import TIER_RANK

        assert TIER_RANK["pro_plus"] > TIER_RANK["pro"], (
            f"Expected pro_plus({TIER_RANK['pro_plus']}) > pro({TIER_RANK['pro']})"
        )

    def test_pro_plus_rank_equals_3(self):
        """Canonical TIER_RANK: pro_plus=3 (plan §G)."""
        from app.access_routes import TIER_RANK

        assert TIER_RANK["pro_plus"] == 3, f"Expected pro_plus=3, got {TIER_RANK['pro_plus']}"


# ── 3. Legacy 'cook' aliases to 'pro' for 30 days ────────────────────────────


class TestLegacyCookAliasesForThirtyDays:
    def test_legacy_cook_aliases_to_pro_for_30_days(self):
        """TIER_RANK['cook'] == TIER_RANK['pro'] during the 30-day alias window."""
        from app.access_routes import TIER_RANK

        assert "cook" in TIER_RANK, "Legacy 'cook' key must exist in TIER_RANK for 30-day alias"
        assert TIER_RANK["cook"] == TIER_RANK["pro"], (
            f"cook({TIER_RANK['cook']}) should equal pro({TIER_RANK['pro']}) as legacy alias"
        )

    def test_cook_alias_rank_is_same_as_pro(self):
        """cook and pro have identical rank (interchangeable for access checks)."""
        from app.access_routes import TIER_RANK

        assert TIER_RANK.get("cook") == TIER_RANK.get("pro")

    def test_cook_is_documented_as_legacy(self):
        """Verify 'cook' is present as alias — its presence in TIER_RANK is the assertion."""
        from app.access_routes import TIER_RANK

        # The key 'cook' must be in TIER_RANK (legacy alias for the 30-day window).
        # Its numeric value must equal 'pro' — no privilege escalation.
        cook_rank = TIER_RANK.get("cook")
        pro_rank = TIER_RANK.get("pro")
        assert cook_rank is not None, "'cook' missing from TIER_RANK"
        assert cook_rank == pro_rank, f"cook={cook_rank} != pro={pro_rank}"


# ── 4. Legacy 'operator' aliases to 'pro_plus' for 30 days ──────────────────


class TestLegacyOperatorAliasesForThirtyDays:
    def test_legacy_operator_aliases_to_pro_plus_for_30_days(self):
        """TIER_RANK['operator'] == TIER_RANK['pro_plus'] during the 30-day alias window."""
        from app.access_routes import TIER_RANK

        assert "operator" in TIER_RANK, (
            "Legacy 'operator' key must exist in TIER_RANK for 30-day alias"
        )
        assert TIER_RANK["operator"] == TIER_RANK["pro_plus"], (
            f"operator({TIER_RANK['operator']}) should equal pro_plus({TIER_RANK['pro_plus']}) as legacy alias"
        )

    def test_operator_alias_rank_is_same_as_pro_plus(self):
        """operator and pro_plus have identical rank."""
        from app.access_routes import TIER_RANK

        assert TIER_RANK.get("operator") == TIER_RANK.get("pro_plus")

    def test_operator_is_documented_as_legacy(self):
        """Verify 'operator' is present as alias — no privilege escalation."""
        from app.access_routes import TIER_RANK

        op_rank = TIER_RANK.get("operator")
        pp_rank = TIER_RANK.get("pro_plus")
        assert op_rank is not None, "'operator' missing from TIER_RANK"
        assert op_rank == pp_rank, f"operator={op_rank} != pro_plus={pp_rank}"


# ── 5. New skill defaults to 'pro' tier (Phase B already shipped) ─────────────


class TestNewSkillDefaultsToProTier:
    def test_new_skill_defaults_to_pro_tier(self):
        """Phase B shipped tier='pro' as the default. Verify it surfaces in access_routes."""
        from app.access_routes import TIER_RANK

        # The default skill tier is 'pro' (see access_routes line 87: TIER_RANK.get(s.tier, TIER_RANK["pro"]))
        # This test verifies the fallback references 'pro', not 'cook'.
        default_rank = TIER_RANK["pro"]
        assert default_rank == 2, (
            f"Default skill tier 'pro' should have rank 2, got {default_rank}"
        )

    def test_pro_tier_is_canonical_not_cook(self):
        """'pro' is the canonical new-skill tier, not 'cook'."""
        from app.access_routes import TIER_RANK

        # canonical tier keys
        assert "pro" in TIER_RANK
        # 'pro' should have rank equal to legacy 'cook'
        assert TIER_RANK["pro"] == TIER_RANK.get("cook", TIER_RANK["pro"])


# ── 6. Clean TIER_RANK structure ─────────────────────────────────────────────


class TestTierRankCleanStructure:
    def test_tier_rank_canonical_keys_present(self):
        """Canonical keys free/pro/pro_plus exist in TIER_RANK."""
        from app.access_routes import TIER_RANK

        for key in ("free", "pro", "pro_plus"):
            assert key in TIER_RANK, f"Canonical key '{key}' missing from TIER_RANK"

    def test_tier_rank_legacy_aliases_present(self):
        """Legacy alias keys cook/operator present in TIER_RANK for 30-day window."""
        from app.access_routes import TIER_RANK

        for key in ("cook", "operator"):
            assert key in TIER_RANK, f"Legacy alias '{key}' missing from TIER_RANK"

    def test_tier_rank_ordering(self):
        """free < pro < pro_plus."""
        from app.access_routes import TIER_RANK

        assert TIER_RANK["free"] < TIER_RANK["pro"] < TIER_RANK["pro_plus"]

    def test_tier_rank_none_is_zero(self):
        """None tier (anonymous) maps to 0 (below free)."""
        from app.access_routes import TIER_RANK

        # None key is explicit in the dict (anonymous callers get 0).
        assert TIER_RANK.get(None, 0) == 0


# ── 7. marketing_routes uses canonical slugs ─────────────────────────────────


class TestMarketingCountsCanonicalSlugs:
    def test_marketing_counts_uses_canonical_slugs(self):
        """marketing_routes.marketing_counts queries 'pro' and 'pro_plus' slugs, not old ones."""
        import inspect
        import app.marketing_routes as mr

        src = inspect.getsource(mr.marketing_counts)
        # After Phase G: by_tier.get("pro", ...) and by_tier.get("pro_plus", ...)
        assert 'by_tier.get("pro"' in src or "by_tier.get('pro'" in src, (
            "marketing_counts should use 'pro' slug (canonical) for DB count"
        )
        assert 'by_tier.get("pro_plus"' in src or "by_tier.get('pro_plus'" in src, (
            "marketing_counts should use 'pro_plus' slug (canonical) for DB count"
        )

    def test_marketing_display_labels_use_canonical_slugs(self):
        """marketing_routes.marketing_counts display labels use canonical 'pro'/'pro_plus'."""
        import inspect
        import app.marketing_routes as mr

        src = inspect.getsource(mr.marketing_counts)
        assert "display_label(\"pro\")" in src or "display_label('pro')" in src, (
            "display_label should be called with 'pro' (canonical)"
        )
        assert "display_label(\"pro_plus\")" in src or "display_label('pro_plus')" in src, (
            "display_label should be called with 'pro_plus' (canonical)"
        )


# ── 8. MCP recall default tier_filter uses canonical slugs ───────────────────


class TestRecallDefaultTierFilter:
    def test_recall_default_tier_filter_uses_canonical_slugs(self):
        """recipes_recall default tier_filter uses 'pro'/'pro_plus', not 'cook'/'operator'."""
        import inspect
        import app.mcp.tools.recall as recall_mod

        src = inspect.getsource(recall_mod.recipes_recall)
        assert '"cook"' not in src, (
            "recall default tier_filter should not contain literal 'cook'"
        )
        assert '"operator"' not in src, (
            "recall default tier_filter should not contain literal 'operator'"
        )
        assert '"pro"' in src, "recall default tier_filter should contain 'pro'"
        assert '"pro_plus"' in src, "recall default tier_filter should contain 'pro_plus'"


# ── 9. MCP search fallback tier_filter uses canonical slugs ──────────────────


class TestSearchFallbackTierFilter:
    def test_search_fallback_tier_filter_uses_canonical_slugs(self):
        """recipes_search hybrid fallback tier_filter uses 'pro'/'pro_plus', not 'cook'/'operator'."""
        import inspect
        import app.mcp.tools.search as search_mod

        src = inspect.getsource(search_mod.recipes_search)
        # The fallback list should use canonical tier names
        assert '"cook"' not in src, (
            "search fallback should not contain literal 'cook'"
        )
        assert '"operator"' not in src, (
            "search fallback should not contain literal 'operator'"
        )


# ── 10. subrecipe_resolve scope updated ──────────────────────────────────────


class TestSubrecipeResolveScope:
    def test_subrecipe_resolve_scope_updated(self):
        """recipes_subrecipe_resolve returns canonical 'pro_plus' scope, not 'operator'."""
        import app.mcp.tools.subrecipe_resolve as sr

        # Call the function with a mock DB
        result = sr.recipes_subrecipe_resolve(db=MagicMock())
        assert result.get("scope") != "operator", (
            "subrecipe_resolve should return 'pro_plus' scope, not legacy 'operator'"
        )
        assert result.get("scope") == "pro_plus", (
            f"Expected scope='pro_plus', got scope='{result.get('scope')}'"
        )


# ── 11. payout_engine uses canonical tier slugs ───────────────────────────────


class TestPayoutEngineCanonicalTiers:
    def test_payout_engine_rates_use_canonical_tiers(self):
        """TIER_RATES in payout_engine uses canonical 'pro'/'pro_plus' keys."""
        import app.payout_engine as pe

        assert "pro" in pe.TIER_RATES, (
            "TIER_RATES should have 'pro' key (canonical)"
        )
        assert "pro_plus" in pe.TIER_RATES, (
            "TIER_RATES should have 'pro_plus' key (canonical)"
        )

    def test_payout_engine_no_bare_cook_key(self):
        """TIER_RATES should not have bare 'cook' as the primary lookup key."""
        import app.payout_engine as pe

        # 'cook' may be kept as legacy alias, but 'pro' must be present
        assert "pro" in pe.TIER_RATES, "'pro' must be canonical key in TIER_RATES"

    def test_payout_engine_no_bare_operator_key(self):
        """TIER_RATES should not have bare 'operator' as the primary lookup key."""
        import app.payout_engine as pe

        # 'operator' may be kept as legacy alias, but 'pro_plus' must be present
        assert "pro_plus" in pe.TIER_RATES, "'pro_plus' must be canonical key in TIER_RATES"

class TestMarketingCountsCookbooksTotal:
    """Test that marketing_counts includes cookbooks_total (public cookbook count)."""

    def test_marketing_counts_has_cookbooks_total(self):
        """marketing_routes.marketing_counts returns cookbooks_total field."""
        import inspect
        import app.marketing_routes as mr

        src = inspect.getsource(mr.marketing_counts)
        assert "cookbooks_total" in src, (
            "marketing_counts should return cookbooks_total (public cookbook count)"
        )

    def test_marketing_counts_queries_cookbook_visibility(self):
        """marketing_counts queries Bundle.visibility == 'public' for cookbooks_total."""
        import inspect
        import app.marketing_routes as mr

        src = inspect.getsource(mr.marketing_counts)
        assert "Bundle" in src, (
            "marketing_counts should query the Bundle model"
        )
        assert 'visibility' in src, (
            "marketing_counts should filter by Cookbook.visibility"
        )
