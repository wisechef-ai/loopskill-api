"""EU VAT MOSS calculation for digital services sold cross-border.

VAT MOSS (Mini One Stop Shop) requires collecting VAT at the buyer's
country rate when selling digital services to EU consumers.

Reference: https://ec.europa.eu/taxation_customs/business/vat/telecommunications-broadcasting-electronic-services_en

Strategy for WiseRecipes:
- B2B: Reverse charge (no VAT) if valid EU VAT number provided
- B2C: Charge VAT at buyer's country rate
- Non-EU: No VAT (outside scope)
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# EU VAT rates as of 2026 (standard rates for digital services)
# Some countries have reduced rates for certain digital goods, but we use standard
EU_VAT_RATES: dict[str, float] = {
    "AT": 0.20,  # Austria
    "BE": 0.21,  # Belgium
    "BG": 0.20,  # Bulgaria
    "HR": 0.25,  # Croatia
    "CY": 0.19,  # Cyprus
    "CZ": 0.21,  # Czech Republic
    "DK": 0.25,  # Denmark
    "EE": 0.22,  # Estonia
    "FI": 0.255, # Finland
    "FR": 0.20,  # France
    "DE": 0.19,  # Germany
    "GR": 0.24,  # Greece
    "HU": 0.27,  # Hungary
    "IE": 0.23,  # Ireland
    "IT": 0.22,  # Italy
    "LV": 0.21,  # Latvia
    "LT": 0.21,  # Lithuania
    "LU": 0.17,  # Luxembourg
    "MT": 0.18,  # Malta
    "NL": 0.21,  # Netherlands
    "PL": 0.23,  # Poland
    "PT": 0.23,  # Portugal
    "RO": 0.19,  # Romania
    "SK": 0.20,  # Slovakia
    "SI": 0.22,  # Slovenia
    "ES": 0.21,  # Spain
    "SE": 0.25,  # Sweden
}

EU_COUNTRY_CODES = set(EU_VAT_RATES.keys())


@dataclass
class VATResult:
    """Result of VAT calculation."""
    country_code: str
    is_eu: bool
    is_b2b: bool
    vat_rate: float
    vat_cents: int
    gross_cents: int  # original amount
    net_cents: int    # amount after VAT removal
    reverse_charge: bool
    vat_number: Optional[str] = None


def calculate_vat(
    gross_amount_cents: int,
    buyer_country_code: str,
    is_b2b: bool = False,
    vat_number: Optional[str] = None,
) -> VATResult:
    """Calculate VAT MOSS for a digital service sale.

    Args:
        gross_amount_cents: The total amount in cents (e.g., 4999 = €49.99)
        buyer_country_code: ISO 3166-1 alpha-2 country code
        is_b2b: Whether this is a B2B transaction
        vat_number: EU VAT number (for B2B reverse charge validation)

    Returns:
        VATResult with breakdown
    """
    country = buyer_country_code.upper() if buyer_country_code else ""
    is_eu = country in EU_COUNTRY_CODES

    # Non-EU: no VAT
    if not is_eu:
        return VATResult(
            country_code=country,
            is_eu=False,
            is_b2b=is_b2b,
            vat_rate=0.0,
            vat_cents=0,
            gross_cents=gross_amount_cents,
            net_cents=gross_amount_cents,
            reverse_charge=False,
            vat_number=vat_number,
        )

    # EU B2B with valid VAT number: reverse charge
    if is_b2b and vat_number:
        # Basic VAT number format validation (country code + digits)
        if _validate_vat_number(country, vat_number):
            return VATResult(
                country_code=country,
                is_eu=True,
                is_b2b=True,
                vat_rate=0.0,
                vat_cents=0,
                gross_cents=gross_amount_cents,
                net_cents=gross_amount_cents,
                reverse_charge=True,
                vat_number=vat_number,
            )

    # EU B2C (or B2B without valid VAT): charge at buyer's country rate
    vat_rate = EU_VAT_RATES.get(country, 0.23)
    # gross_amount includes VAT, so we need to extract:
    # net = gross / (1 + rate)
    # vat = gross - net
    net_cents = round(gross_amount_cents / (1 + vat_rate))
    vat_cents = gross_amount_cents - net_cents

    return VATResult(
        country_code=country,
        is_eu=True,
        is_b2b=is_b2b,
        vat_rate=vat_rate,
        vat_cents=vat_cents,
        gross_cents=gross_amount_cents,
        net_cents=net_cents,
        reverse_charge=False,
        vat_number=vat_number,
    )


def _validate_vat_number(country_code: str, vat_number: str) -> bool:
    """Basic EU VAT number format validation.

    Production should use VIES (VAT Information Exchange System) for real-time validation.
    This does a basic format check: country code prefix + 5-15 alphanumeric chars.
    """
    if not vat_number:
        return False

    # Strip spaces and common prefixes
    cleaned = vat_number.strip().replace(" ", "").replace("-", "").upper()

    # Should start with country code
    if not cleaned.startswith(country_code):
        # Some countries use different prefixes (e.g., Greece uses EL)
        if country_code == "GR" and cleaned.startswith("EL"):
            pass
        else:
            return False

    # After prefix, should be 5-15 alphanumeric characters
    number_part = cleaned[len(country_code):]
    if len(number_part) < 5 or len(number_part) > 15:
        return False

    if not number_part.isalnum():
        return False

    return True


def generate_vat_moss_report(payouts_by_country: dict[str, int]) -> list[dict]:
    """Generate a VAT MOSS report for a given period.

    Args:
        payouts_by_country: {country_code: total_gross_cents}

    Returns:
        List of line items for VAT MOSS filing
    """
    report = []
    for country, gross_cents in sorted(payouts_by_country.items()):
        if country not in EU_COUNTRY_CODES:
            continue
        vat_rate = EU_VAT_RATES[country]
        net_cents = round(gross_cents / (1 + vat_rate))
        vat_cents = gross_cents - net_cents

        report.append({
            "country_code": country,
            "vat_rate": round(vat_rate * 100, 1),
            "gross_cents": gross_cents,
            "net_cents": net_cents,
            "vat_cents": vat_cents,
            "gross_eur": round(gross_cents / 100, 2),
            "net_eur": round(net_cents / 100, 2),
            "vat_eur": round(vat_cents / 100, 2),
        })

    return report
