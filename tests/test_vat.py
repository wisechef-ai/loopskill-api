"""Tests for VAT MOSS calculation."""

import pytest

from app.vat import (
    EU_VAT_RATES,
    VATResult,
    _validate_vat_number,
    calculate_vat,
    generate_vat_moss_report,
)


class TestCalculateVAT:
    """Test VAT calculation for various scenarios."""

    def test_non_eu_country_no_vat(self):
        """Non-EU buyers pay no VAT."""
        result = calculate_vat(10000, "US", is_b2b=False)
        assert result.is_eu is False
        assert result.vat_rate == 0.0
        assert result.vat_cents == 0
        assert result.net_cents == 10000
        assert result.reverse_charge is False

    def test_poland_b2c_standard_rate(self):
        """Polish B2C buyer pays 23% VAT."""
        result = calculate_vat(10000, "PL", is_b2b=False)
        assert result.is_eu is True
        assert result.vat_rate == 0.23
        assert result.net_cents == 8130  # 10000 / 1.23
        assert result.vat_cents == 1870  # 10000 - 8130
        assert result.reverse_charge is False

    def test_germany_b2c(self):
        """German B2C buyer pays 19% VAT."""
        result = calculate_vat(10000, "DE", is_b2b=False)
        assert result.vat_rate == 0.19
        assert result.net_cents == 8403  # 10000 / 1.19
        assert result.vat_cents == 1597

    def test_b2b_reverse_charge_with_vat_number(self):
        """EU B2B with valid VAT number = reverse charge (0% VAT)."""
        result = calculate_vat(10000, "DE", is_b2b=True, vat_number="DE123456789")
        assert result.is_eu is True
        assert result.vat_rate == 0.0
        assert result.vat_cents == 0
        assert result.net_cents == 10000
        assert result.reverse_charge is True

    def test_b2b_without_vat_number_charges_vat(self):
        """EU B2B without VAT number = still charges VAT."""
        result = calculate_vat(10000, "DE", is_b2b=True, vat_number=None)
        assert result.vat_rate == 0.19
        assert result.vat_cents > 0
        assert result.reverse_charge is False

    def test_all_eu_countries_have_rates(self):
        """All 27 EU member states have VAT rates defined."""
        assert len(EU_VAT_RATES) == 27
        for country, rate in EU_VAT_RATES.items():
            assert 0 < rate < 0.30, f"{country} has unexpected VAT rate: {rate}"

    def test_unknown_eu_country_uses_default(self):
        """Unknown country code treated as non-EU."""
        result = calculate_vat(10000, "XX", is_b2b=False)
        assert result.is_eu is False
        assert result.vat_rate == 0.0

    def test_greece_vat_rate(self):
        """Greece (GR) has 24% rate."""
        result = calculate_vat(10000, "GR", is_b2b=False)
        assert result.vat_rate == 0.24

    def test_empty_country_no_vat(self):
        """Empty country code treated as non-EU."""
        result = calculate_vat(10000, "", is_b2b=False)
        assert result.is_eu is False

    def test_small_amount(self):
        """Even small amounts get correct VAT treatment."""
        result = calculate_vat(100, "PL", is_b2b=False)
        assert result.vat_rate == 0.23
        assert result.net_cents == 81
        assert result.vat_cents == 19


class TestVATNumberValidation:
    def test_valid_german_vat(self):
        assert _validate_vat_number("DE", "DE123456789") is True

    def test_valid_polish_vat(self):
        assert _validate_vat_number("PL", "PL1234567890") is True

    def test_mismatched_country(self):
        assert _validate_vat_number("DE", "PL1234567890") is False

    def test_too_short(self):
        assert _validate_vat_number("DE", "DE123") is False

    def test_greece_uses_el_prefix(self):
        """Greece uses EL prefix instead of GR."""
        assert _validate_vat_number("GR", "EL123456789") is True

    def test_empty_vat_number(self):
        assert _validate_vat_number("DE", "") is False

    def test_none_vat_number(self):
        assert _validate_vat_number("DE", None) is False


class TestVATMOSSReport:
    def test_empty_report(self):
        report = generate_vat_moss_report({})
        assert report == []

    def test_single_country_report(self):
        report = generate_vat_moss_report({"PL": 100000})  # €1000
        assert len(report) == 1
        assert report[0]["country_code"] == "PL"
        assert report[0]["vat_rate"] == 23.0
        assert report[0]["vat_cents"] > 0

    def test_multi_country_report(self):
        data = {"PL": 100000, "DE": 50000, "US": 30000}
        report = generate_vat_moss_report(data)
        # US should be excluded
        assert len(report) == 2
        countries = [r["country_code"] for r in report]
        assert "DE" in countries
        assert "PL" in countries
        assert "US" not in countries

    def test_report_sorted_by_country(self):
        data = {"PL": 100000, "DE": 50000, "FR": 75000}
        report = generate_vat_moss_report(data)
        countries = [r["country_code"] for r in report]
        assert countries == sorted(countries)
