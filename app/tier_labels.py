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


def _tier_bundle_cap(cfg: dict) -> int | None:
    """Read a tier's bundle cap from config, preferring the canonical key.

    Accepts the new ``bundle_limit`` key and falls back to the legacy
    ``cookbook_limit`` key so config and code can roll independently during the
    cookbook→bundle rename. The literal ``0`` default is a fail-closed guard for
    a missing/corrupt key only.
    """
    if "bundle_limit" in cfg:
        return cfg.get("bundle_limit", 0)
    return cfg.get("cookbook_limit", 0)


def bundle_limit(tier: str | None) -> int | None:
    """Return the max number of bundles a tier may own.

    SSOT: config/tiers.yaml `bundle_limit` per tier (legacy key `cookbook_limit`
    still honored via _tier_bundle_cap). This is the ONLY source of bundle caps
    — bundle_routes.py and auth_routes.py both read it here. Accepts legacy
    slugs ('cook', 'studio', 'operator') transparently via _canonical().

    Returns an int cap, or None for unlimited (reserved; no current tier is
    unlimited). Unknown/None tier falls back to the free-tier limit from the
    SSOT (config/tiers.yaml). The literal ``0`` defaults are a fail-closed guard
    for a missing/corrupt config file only — never the live free value (which is
    read from YAML; liked_0711 set it to 2).
    """
    canonical = _canonical(tier) if tier else "free"
    tier_cfg = _tiers().get(canonical)
    if tier_cfg is None:
        # Unknown tier → safest is the free-tier cap.
        return _tier_bundle_cap(_tiers().get("free", {}))
    return _tier_bundle_cap(tier_cfg)


# Back-compat alias for the cookbook→bundle rename. Callers should migrate to
# bundle_limit(); this keeps older imports working during the rollout.
cookbook_limit = bundle_limit


# DEFAULT_API_KEY_CAP is the fail-closed fallback for a missing/corrupt
# `api_key_cap` key AND for an unknown/None tier (bundles_0811 P2.5, Adam
# 2026-08-10 lock #11). Deliberately a HARDCODED literal, not a read of the
# free-tier YAML value: an unknown/null tier must remain the most restrictive
# cap under every circumstance, including a future edit that raises Free —
# it must never silently inherit a raise via this fallback. This is the one
# tier number allowed to live outside config/tiers.yaml, and only because it
# is not a tier's number at all — it is the "we don't know the tier" guard.
DEFAULT_API_KEY_CAP = 1


def api_key_cap(tier: str | None) -> int:
    """Return the max active API keys a tier may hold.

    SSOT: config/tiers.yaml `api_key_cap` per tier. This is the ONLY source of
    active-key caps — app/api_key_routes.py reads it here rather than holding
    its own Python-literal dict (the bundles_0811 P2.5 fix for the verified
    defect where Pro shared Free's cap of 1). Accepts legacy slugs ('cook',
    'studio', 'operator') transparently via _canonical().

    Returns DEFAULT_API_KEY_CAP (1) for an unknown/None tier or a tier config
    missing the key — the most restrictive cap, by design, never inherited
    from another tier's value.
    """
    if not tier:
        return DEFAULT_API_KEY_CAP
    canonical = _canonical(tier)
    tier_cfg = _tiers().get(canonical)
    if tier_cfg is None:
        return DEFAULT_API_KEY_CAP
    cap = tier_cfg.get("api_key_cap")
    if cap is None:
        return DEFAULT_API_KEY_CAP
    return cap


def is_public_tier(tier: str | None) -> bool:
    """Return False only for a tier explicitly flagged `public: false` in the SSOT.

    autopilot_0308 M2 / D-003: the public ladder is exactly 3 tiers (Free /
    Pro / Enterprise-on-demand). pro_plus carries `public: false` in
    config/tiers.yaml so it drops out of /pricing and marketing copy while
    staying a fully valid, resolvable db_slug (bundle_limit(), display_label()
    keep working) for the migration window and Enterprise contracts.

    Defaults to True (public) for an unknown/None tier — presentation should
    fail open, never silently hide a real tier because of a lookup typo.
    """
    if not tier:
        return True
    canonical = _canonical(tier)
    tier_cfg = _tiers().get(canonical)
    if tier_cfg is None:
        return True
    return tier_cfg.get("public", True) is not False


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
