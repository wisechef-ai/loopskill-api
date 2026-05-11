"""Tier label helpers — load from config/tiers.yaml.
For any user-facing text (API responses, emails, logs), use display_label(slug)
instead of hardcoding 'Cook' or 'Operator'.

Helpers _is_paid_tier() and _is_operator_tier() handle legacy 'studio' slug
transparently for the 30-day backwards-compat window (RCP-INCIDENT-2026-05-11).
"""
import yaml
from functools import lru_cache
from pathlib import Path

# config/tiers.yaml lives two levels up from app/
TIERS_YAML = Path(__file__).resolve().parent.parent / 'config' / 'tiers.yaml'

# Legacy slug mapping — 'studio' was the original DB slug for the operator tier.
# Accepted transparently for 30 days after migration (remove after 2026-06-10).
# RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
_LEGACY_SLUG_MAP: dict[str, str] = {
    "studio": "operator",
}


@lru_cache(maxsize=1)
def _tiers() -> dict:
    with open(TIERS_YAML) as f:
        return yaml.safe_load(f)['tiers']


def _canonical(slug: str) -> str:
    """Resolve legacy slugs to their canonical form."""
    return _LEGACY_SLUG_MAP.get(slug, slug)


def display_label(db_slug: str) -> str:
    """Return the user-facing display name for a DB tier slug.

    Accepts legacy slug 'studio' transparently (maps to 'operator').
    """
    canonical = _canonical(db_slug)
    return _tiers().get(canonical, {}).get('display_name', canonical.title())


def _is_paid_tier(tier: str | None) -> bool:
    """Return True if tier is any paid tier (cook, operator, or legacy studio).

    # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
    """
    if not tier:
        return False
    return _canonical(tier) in ("cook", "operator")


def _is_operator_tier(tier: str | None) -> bool:
    """Return True if tier is the operator tier (or legacy 'studio' slug).

    # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
    """
    if not tier:
        return False
    return _canonical(tier) == "operator"
