"""Tier label helpers — load from config/tiers.yaml.
For any user-facing text (API responses, emails, logs), use display_label(slug)
instead of hardcoding 'Pro' or 'Pro+'.

Helpers _is_paid_tier() and _is_pro_plus_tier() handle legacy slugs
transparently for the 30-day backwards-compat window (RCP-INCIDENT-2026-05-11).
"""

import logging
from functools import lru_cache
from pathlib import Path

import yaml

# config/tiers.yaml lives two levels up from app/
TIERS_YAML = Path(__file__).resolve().parent.parent / "config" / "tiers.yaml"

logger = logging.getLogger(__name__)

# Legacy slug mapping — tracks all pre-Phase-5 DB slugs to canonical.
# Accepted transparently for 30 days after migration (remove after 2026-06-10).
# RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
_LEGACY_SLUG_MAP: dict[str, str] = {
    "studio": "pro_plus",  # Phase 3 legacy
    "operator": "pro_plus",  # Phase 5 legacy
    "cook": "pro",  # Phase 5 legacy
}


@lru_cache(maxsize=1)
def _tiers() -> dict:
    with open(TIERS_YAML) as f:
        return yaml.safe_load(f)["tiers"]


def _canonical(slug: str) -> str:
    """Resolve legacy slugs to their canonical form."""
    return _LEGACY_SLUG_MAP.get(slug, slug)


def display_label(db_slug: str) -> str:
    """Return the user-facing display name for a DB tier slug.

    Accepts legacy slugs 'studio', 'operator', 'cook' transparently.
    """
    canonical = _canonical(db_slug)
    return _tiers().get(canonical, {}).get("display_name", canonical.title())


def cookbook_limit(tier: str | None) -> int | None:
    """Return the max number of cookbooks a tier may own.

    SSOT: config/tiers.yaml `cookbook_limit` per tier (loopclose_3005 Phase A).
    This is the ONLY source of bundle caps — bundle_routes.py and
    auth_routes.py both read it here. Accepts legacy slugs ('cook', 'studio',
    'operator') transparently via _canonical().

    Returns an int cap, or None for unlimited (reserved; no current tier is
    unlimited). Unknown/None tier falls back to the free-tier limit from the
    SSOT (config/tiers.yaml). The literal ``0`` defaults below are a
    fail-closed guard for a missing/corrupt config file only — never the live
    free value (which is read from YAML; evergreen_0206 Phase A set it to 1).
    """
    canonical = _canonical(tier) if tier else "free"
    tier_cfg = _tiers().get(canonical)
    if tier_cfg is None:
        # Unknown tier → safest is the free-tier cap.
        return _tiers().get("free", {}).get("cookbook_limit", 0)
    return tier_cfg.get("cookbook_limit", 0)


def _is_paid_tier(tier: str | None) -> bool:
    """Return True if tier is any paid tier (pro, pro_plus, or legacy slugs).

    # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
    """
    if not tier:
        return False
    return _canonical(tier) in ("pro", "pro_plus")


def _is_pro_tier(tier: str | None) -> bool:
    """Return True if tier is pro or above (pro, pro_plus, or legacy slugs).

    Used by integrator_2905 W1 to gate fork/tailor access at the Pro tier
    (not pro_plus) for broader first-dollar funnel.
    """
    if not tier:
        return False
    return _canonical(tier) in ("pro", "pro_plus")


def _is_pro_plus_tier(tier: str | None) -> bool:
    """Return True if tier is the pro_plus tier (or legacy 'operator'/'studio' slugs).

    # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
    """
    if not tier:
        return False
    return _canonical(tier) == "pro_plus"


def _is_operator_tier(tier: str | None) -> bool:
    """Deprecated wrapper — delegates to _is_pro_plus_tier.

    # RCP-INCIDENT-2026-05-11 backwards-compat shim, remove after 2026-06-10
    Kept for any external caller that imports the old function name.
    All internal callers have been updated to use _is_pro_plus_tier directly.
    """
    logger.debug(
        "DEPRECATION: _is_operator_tier() called — use _is_pro_plus_tier() instead. "
        "This wrapper will be removed after 2026-06-10 (RCP-INCIDENT-2026-05-11)."
    )
    return _is_pro_plus_tier(tier)
