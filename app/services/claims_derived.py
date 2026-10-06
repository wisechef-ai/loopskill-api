"""Retired-claim rules DERIVED from config/tiers.yaml (split from
claims_contract for the 600-line module cap). Each rule exists only while
tiers.yaml makes the claim false, so editing tiers.yaml retires or revives it.
Rules are data: install.sql runs them unchanged via the contract sync."""

import re

from app.services.claims_numbers import _KEY_UNIT


def _derived_retired(tiers: dict) -> list[dict]:
    """One retired rule per non-public tier: its display name, badge and slug."""
    rules = []
    for slug, cfg in tiers.items():
        if cfg.get("public", True):
            continue
        names = {slug}
        for key in ("display_name", "badge"):
            if cfg.get(key):
                names.add(str(cfg[key]))
        if str(cfg.get("display_name", "")).endswith("+"):
            names.add(str(cfg["display_name"])[:-1] + " Plus")
        alts = sorted({re.escape(n).replace(r"\ ", " ") for n in names}, key=len, reverse=True)
        rules.append(
            {
                "id": f"tier-not-public-{slug}",
                "pattern": r"\b(" + "|".join(alts) + r")",
                "reason": (
                    f"Tier {cfg.get('display_name', slug)!r} is public: false in config/tiers.yaml; "
                    "it is not on the public ladder and must not be advertised."
                ),
                "replacement": "the public tiers listed in this contract",
                "source": "config/tiers.yaml",
            }
        )
    return rules


def derived_fact_rules(public: list[dict]) -> list[dict]:
    """Claims no public tier supports: annual billing, unlimited private
    bundles, unlimited API keys (rounds 21-22)."""
    rules = []
    if all(t.get("annual_price_usd") is None for t in public):
        rules.append(
            {
                "id": "annual-billing-not-offered",
                "pattern": r"\b((billed|paid|charged|invoiced|payable) (annually|yearly|per year|once a year|per annum)"
                r"|pay (annually|yearly)|per annum"
                r"|(annual|yearly) (billing|plans?|subscriptions?|pricing|commitment|contracts?|payments?))\b",
                "reason": "no tier in config/tiers.yaml has an annual_price_usd: LoopSkill bills monthly",
                "replacement": "per-month pricing from app.loopskill.io/pricing",
                "source": "config/tiers.yaml",
            }
        )
    many = r"(unlimited|infinite|limitless|uncapped|endless) "
    as_many = r"as many "
    want = r" as you (want|like|need)\b"
    for key, unit, rid in (
        ("bundle_limit", r"private bundles?", "unlimited-private-bundles"),
        ("api_key_cap", _KEY_UNIT, "unlimited-api-keys"),
    ):
        if all(t.get(key) is not None for t in public):
            rules.append(
                {
                    "id": rid,
                    "pattern": r"\b("
                    + many
                    + unit
                    + r"|no (limit|cap) on "
                    + unit
                    + "|"
                    + as_many
                    + unit
                    + want
                    + ")",
                    "reason": f"every public tier in config/tiers.yaml has a finite {key}",
                    "replacement": "the tier's cap from app.loopskill.io/pricing",
                    "source": "config/tiers.yaml",
                }
            )
    return rules
