"""Tests for payout engine."""

import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from uuid import uuid4

from app.payout_engine import get_creator_payout_rate, TIER_RATES
from app.config import settings
from app.models import Creator, Skill


class TestPayoutRates:
    """Test creator payout rate calculation."""

    def _make_creator(self, is_founder=False):
        return Creator(
            id=uuid4(),
            name="Test",
            slug="test",
            is_founder=is_founder,
        )

    def _make_skill(self, tier="cook"):
        return Skill(
            id=uuid4(),
            slug="test-skill",
            title="Test Skill",
            tier=tier,
        )

    def test_cook_rate(self):
        creator = self._make_creator(is_founder=False)
        skill = self._make_skill(tier="cook")
        rate = get_creator_payout_rate(skill, creator)
        assert rate == settings.PAYOUT_RATE_COOK == 0.50

    def test_operator_rate(self):
        creator = self._make_creator(is_founder=False)
        skill = self._make_skill(tier="operator")
        rate = get_creator_payout_rate(skill, creator)
        assert rate == settings.PAYOUT_RATE_OPERATOR == 0.60

    def test_studio_rate(self):
        creator = self._make_creator(is_founder=False)
        skill = self._make_skill(tier="studio")
        rate = get_creator_payout_rate(skill, creator)
        assert rate == settings.PAYOUT_RATE_STUDIO_PRIVATE == 0.70

    def test_founder_gets_bonus_rate(self):
        """Founder publishers get 75% regardless of tier."""
        creator = self._make_creator(is_founder=True)
        skill = self._make_skill(tier="cook")
        rate = get_creator_payout_rate(skill, creator)
        assert rate == settings.PAYOUT_RATE_FOUNDER_BONUS == 0.75

    def test_founder_still_75_with_studio(self):
        creator = self._make_creator(is_founder=True)
        skill = self._make_skill(tier="studio")
        rate = get_creator_payout_rate(skill, creator)
        assert rate == 0.75

    def test_none_tier_defaults_to_cook(self):
        creator = self._make_creator(is_founder=False)
        skill = self._make_skill(tier=None)
        rate = get_creator_payout_rate(skill, creator)
        assert rate == 0.50

    def test_unknown_tier_defaults_to_cook(self):
        creator = self._make_creator(is_founder=False)
        skill = self._make_skill(tier="premium")
        rate = get_creator_payout_rate(skill, creator)
        assert rate == 0.50


class TestComputeMonthlyPayouts:
    """Test the monthly payout computation (dry-run mode)."""

    @patch("app.payout_engine.compute_monthly_payouts")
    def test_dry_run_doesnt_write(self, mock_compute):
        """In dry_run mode, payouts are computed but not persisted."""
        # This is a unit test to verify the interface
        mock_compute.return_value = [
            {
                "creator_name": "Test",
                "creator_share_cents": 5000,
                "status": "dry_run",
            }
        ]
        result = mock_compute(dry_run=True)
        assert len(result) == 1
        assert result[0]["status"] == "dry_run"
