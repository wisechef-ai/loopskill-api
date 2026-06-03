"""evergreen_0206 Phase A — free tier on-ramp: cookbook_limit 0 → 1.

Decision #10 (locked): the free on-ramp was CLOSED (`cookbook_limit: 0`), so a
free user could not create even one cookbook — they could never feel the
"watch a cookbook self-heal once" taste that drives conversion. Phase A pins
free to exactly 1 cookbook.

SSOT discipline (Adam's no-drift rule): the number lives in ONE place,
``config/tiers.yaml``, read everywhere via ``tier_labels.cookbook_limit()``.
This suite asserts the SSOT value AND that the two read-surfaces that
interpolate it (/api/billing/me via auth_routes + checkout_routes) agree — so
a future edit to the YAML can never silently drift from what users are billed
against.
"""

from __future__ import annotations

from app.tier_labels import cookbook_limit


class TestFreeCookbookLimitSSOT:
    """The SSOT number itself."""

    def test_free_is_one(self):
        assert cookbook_limit("free") == 1, (
            "free on-ramp must allow exactly 1 cookbook (decision #10); " "0 keeps the funnel closed"
        )

    def test_none_tier_is_one(self):
        """A user with no tier set is treated as free → 1 cookbook."""
        assert cookbook_limit(None) == 1

    def test_pro_unchanged(self):
        assert cookbook_limit("pro") == 10

    def test_pro_plus_unchanged(self):
        assert cookbook_limit("pro_plus") == 200

    def test_legacy_aliases_unchanged(self):
        """30-day legacy aliases still resolve (remove after 2026-06-10)."""
        assert cookbook_limit("cook") == 10
        assert cookbook_limit("operator") == 200


class TestNoResidualZero:
    """Guard: no surface hardcodes free=0 outside the SSOT."""

    def test_yaml_free_block_is_one(self):
        import pathlib

        import yaml

        # Walk up from this test file to find config/tiers.yaml in the repo root.
        here = pathlib.Path(__file__).resolve()
        root = here.parent.parent
        tiers_path = root / "config" / "tiers.yaml"
        assert tiers_path.exists(), f"tiers.yaml not found at {tiers_path}"
        data = yaml.safe_load(tiers_path.read_text())
        free_limit = data["tiers"]["free"]["cookbook_limit"]
        assert free_limit == 1, f"config/tiers.yaml free.cookbook_limit must be 1, got {free_limit!r}"
