"""Bundle-create conversion gate — the one live rung of evergreen_0206 Phase G.

paywall_0925 removed the other three predicates (``gate_manual_sync``,
``gate_daemon_cron_install``, ``gate_fleet``). They had no caller outside
tests, and the ladder they encoded no longer matches config/tiers.yaml:

  * tiers.yaml has no manual-sync allowance at all (no "one free sync");
  * fleet was "Pro+ only, Pro -> 403", but the live fleet-member cap
    (fleet_member_routes.TIER_KEY_CAPS) gives Pro 200 members, and Pro+ is
    ``public: false``.

Re-introducing a sync paywall is a PRICING decision (lock #24), not a wiring
fix: write it against tiers.yaml first. Every live tier refusal is recorded
through ``app.services.paywall_hits.record_paywall_hit``.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.tier_labels import _is_paid_tier, bundle_limit


@dataclass(frozen=True)
class GateOutcome:
    allowed: bool
    http_status: int  # 200 when allowed; 402 (upgrade) or 403 (forbidden) otherwise
    reason: str
    upgrade_to: str | None = None


def gate_cookbook_create(tier: str | None, current_count: int, limit: int | None) -> GateOutcome:
    """Creating a bundle is allowed up to the tier's SSOT limit.

    evergreen_0206 Phase G OPENS free creation: the hard 401 'paid tier
    required' wall is removed for bundle creation — free users may create up to
    their limit; the count-cap (not a tier wall) enforces it.

    ``current_count`` is the number of METERED bundles, i.e. everything the user
    owns that is not ``visibility='public'`` (autopilot_0308 M1, D-011: public
    bundles are unlimited on every tier). Do not pass a raw owned-bundle count —
    call :func:`gate_bundle_create`, or count via
    ``app.services.bundle_quota.count_metered_bundles``, which is the one
    implementation of that predicate.
    """
    if limit is not None and current_count >= limit:
        return GateOutcome(
            allowed=False,
            http_status=403,
            # `cookbook_limit` is the wire-visible code (MCP error code, portal
            # copy) — kept verbatim; only the human half of the string gained
            # the visibility qualifier that was missing.
            reason=f"cookbook_limit reached ({current_count}/{limit} private bundles; public are unlimited)",
            upgrade_to="pro" if not _is_paid_tier(tier) else "pro_plus",
        )
    return GateOutcome(allowed=True, http_status=200, reason="within private bundle limit")


def gate_bundle_create(db, user_id, tier: str | None) -> GateOutcome:
    """DB-aware :func:`gate_cookbook_create` — counts the user's metered bundles.

    The convenience form for callers that hold a session: it takes the count
    from ``app.services.bundle_quota`` so the conversion ladder meters exactly
    what the REST and MCP enforcers meter.
    """
    from app.services.bundle_quota import count_metered_bundles

    return gate_cookbook_create(
        tier, current_count=count_metered_bundles(db, user_id), limit=bundle_limit(tier)
    )
