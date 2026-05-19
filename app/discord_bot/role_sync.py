"""Map a user's subscription state → Discord role.

Roles:
  pro_plus (active)        → Pro+
  pro      (active)        → Pro
  no/canceled subscription → Free
  creator_track_record_score >= threshold → Author overlay

Only `active` (or `trialing`) subscriptions count — anything else
(canceled, past_due, unpaid, paused) drops the user back to Free.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings

logger = logging.getLogger("wiserecipes.discord")

ACTIVE_STATUSES = frozenset({"active", "trialing"})

# Canonical tier → Discord role name mapping.
# Legacy slug aliases kept for the 30-day shim window (RCP-INCIDENT-2026-05-11, remove after 2026-06-10).
# Do NOT rename Discord role names here — that's a separate ops task.
TIER_TO_ROLE = {
    "pro_plus": "Pro+",  # canonical (Phase 5)
    "pro": "Pro",  # canonical (Phase 5)
    # Legacy aliases:
    "operator": "Pro+",  # Phase 5 legacy
    "studio": "Pro+",  # Phase 3 legacy
    "cook": "Pro",  # Phase 5 legacy
}


def role_for_user(
    user: Any,
    *,
    author_threshold: float | None = None,
    include_overlays: bool = False,
) -> str | list[str]:
    """Return the Discord role(s) for a user.

    Default behaviour returns the *base* role string. Pass
    `include_overlays=True` to receive a list including the Author overlay
    when the user qualifies.
    """
    status = getattr(user, "subscription_status", None)
    tier = getattr(user, "subscription_tier", None)
    base = "Free"
    if status in ACTIVE_STATUSES and tier in TIER_TO_ROLE:
        base = TIER_TO_ROLE[tier]

    if not include_overlays:
        return base

    roles = [base]
    threshold = author_threshold if author_threshold is not None else settings.DISCORD_AUTHOR_THRESHOLD
    score = getattr(user, "creator_track_record_score", None) or 0
    if score >= threshold:
        roles.append("Author")
    return roles


def sync_role_for_user(user: Any, *, client: Any) -> dict:
    """Apply the role to the user's Discord account, if any.

    `client` must expose `assign_role(discord_user_id, role_name)`. When the
    bot is running (lifespan task started), the lifespan injects a real
    client; in tests we pass a Mock.
    """
    discord_id = getattr(user, "discord_user_id", None)
    if not discord_id:
        return {"skipped": "no_discord_user_id"}
    role = role_for_user(user)
    client.assign_role(discord_id, role)
    return {"role": role, "discord_user_id": discord_id}
