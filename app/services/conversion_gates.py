"""Maintenance-gated conversion ladder — evergreen_0206 Phase G.

The paid axis is MAINTENANCE, not ACCESS (decision #1). The ladder:

  FREE  → 1 cookbook + exactly ONE manual sync (the taste). Watch a cookbook
          self-heal + auto-recover once. Then the ceiling is felt.
  PRO   → scheduled/cron auto-reconcile (the daemon). Skills never rot, hands-off.
  PRO+  → fleet reconcile across N agents + channels. The control plane.

This module holds the gate PREDICATES (pure, testable). The reasoning is
front-loaded from A-E (the seams already exist); G just wires the ceilings:
  - free 2nd manual sync       → 402 upgrade
  - free daemon-cron install   → 402 upgrade (scheduled reconcile is Pro)
  - free/pro fleet tools        → 403 (fleet is Pro+)
"""

from __future__ import annotations

from dataclasses import dataclass

from app.tier_labels import _is_paid_tier, _is_pro_plus_tier


@dataclass(frozen=True)
class GateOutcome:
    allowed: bool
    http_status: int  # 200 when allowed; 402 (upgrade) or 403 (forbidden) otherwise
    reason: str
    upgrade_to: str | None = None


def gate_manual_sync(tier: str | None, free_sync_used_at) -> GateOutcome:
    """A manual (human-initiated) reconcile/sync.

    Paid tiers: always allowed (Pro gets cron auto-reconcile anyway). Free: the
    FIRST manual sync is the taste (allowed); a SECOND → 402 upgrade.
    free_sync_used_at is None until the free user has spent their one sync.
    """
    if _is_paid_tier(tier):
        return GateOutcome(allowed=True, http_status=200, reason="paid tier")
    # Free / no tier.
    if free_sync_used_at is None:
        return GateOutcome(
            allowed=True,
            http_status=200,
            reason="free taste: first manual sync",
        )
    return GateOutcome(
        allowed=False,
        http_status=402,
        reason="You watched it self-heal once. Want that on a cron? Pro.",
        upgrade_to="pro",
    )


def gate_daemon_cron_install(tier: str | None) -> GateOutcome:
    """Installing the scheduled reconcile daemon-cron is a PRO capability.

    Free can run ONE manual sync but cannot wire the always-on cron — that's the
    evergreen guarantee they upgrade for.
    """
    if _is_paid_tier(tier):
        return GateOutcome(allowed=True, http_status=200, reason="paid tier")
    return GateOutcome(
        allowed=False,
        http_status=402,
        reason="Scheduled auto-reconcile (skills never rot) is Pro.",
        upgrade_to="pro",
    )


def gate_fleet(tier: str | None) -> GateOutcome:
    """Fleet reconcile across N agents is a PRO+ capability."""
    if _is_pro_plus_tier(tier):
        return GateOutcome(allowed=True, http_status=200, reason="pro_plus tier")
    return GateOutcome(
        allowed=False,
        http_status=403,
        reason="Managing N agents by hand? Pro+.",
        upgrade_to="pro_plus",
    )


def gate_cookbook_create(tier: str | None, current_count: int, limit: int | None) -> GateOutcome:
    """Creating a cookbook is allowed up to the tier's SSOT limit.

    evergreen_0206 Phase G OPENS free creation: free=1 (SSOT, Phase A). The hard
    401 'paid tier required' wall is removed for cookbook creation — free users
    may create up to their limit; the count-cap (not a tier wall) enforces it.
    """
    if limit is not None and current_count >= limit:
        return GateOutcome(
            allowed=False,
            http_status=403,
            reason=f"cookbook_limit reached ({current_count}/{limit})",
            upgrade_to="pro" if not _is_paid_tier(tier) else "pro_plus",
        )
    return GateOutcome(allowed=True, http_status=200, reason="within cookbook limit")
