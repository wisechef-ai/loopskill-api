"""Auto-mint the user's first API key at OAuth signup (flywheel P1, F1.2).

Verified 2026-08-19: 6/11 users (4/7 paying) had ZERO API keys — signup never
gave them one and they had to find the keys page themselves, so their agents
could not authenticate at all. This module mints exactly one standard `rec_`
key the moment a user is seen with zero ``api_keys`` rows EVER.

Idempotency note: rather than threading a "just created" flag through
find_or_create_user_by_github/google (touching a hot, concurrently-edited
path), this checks a persisted DB fact — "does this user have any api_keys
row at all, active or revoked". That fact is true forever once set (revoking
a key never deletes the row), so a returning user's second/third/Nth login
never re-mints, retries are naturally idempotent, and no schema change is
needed. This mirrors the same posture as ``app.liked_service.ensure_liked_bundle``
(idempotent-by-query, not idempotent-by-flag).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models import APIKey, User

logger = logging.getLogger(__name__)

FIRST_KEY_LABEL = "first-key (auto)"


def ensure_first_api_key(db: Session, user: User) -> APIKey | None:
    """Mint the user's first API key iff they have never had one.

    Never raises — any failure (DB error, tier-cap lookup failure, etc.) is
    logged and swallowed so OAuth sign-in is NEVER blocked by key minting
    (explicit spec requirement). Returns the newly created ``APIKey`` row
    with a transient ``.plaintext`` attribute set (NOT a mapped column — the
    plaintext is never persisted, same posture as the existing
    ``POST /api/api-keys`` creation path in ``app.api_key_routes``), or
    ``None`` when a key already existed, minting failed, or the caller's
    tier cap does not allow even one key.
    """
    try:
        has_any_key = db.query(APIKey.id).filter(APIKey.user_id == user.id).first() is not None
        if has_any_key:
            return None

        # Tier-cap discipline (D-038): stay within the SSOT cap even though a
        # brand-new user with zero keys is, in every current tier
        # (config/tiers.yaml), always under cap. Defensive, not decorative —
        # a future tier misconfiguration (cap=0) must not mint an orphan key.
        from app.revenue_truth import entitled_tier_or_free
        from app.tier_labels import api_key_cap as _tier_api_key_cap

        tier = entitled_tier_or_free(user)
        cap = _tier_api_key_cap(tier)
        if cap < 1:
            logger.warning(
                "Skipping first-key auto-mint for user %s: tier %s cap is %d",
                user.id,
                tier,
                cap,
            )
            return None

        # Local import: app.api_key_routes imports app.auth_routes (for
        # get_current_user_optional), and app.auth_routes is the caller of
        # this module — a top-level import here would be circular.
        from app.api_key_routes import _generate_key

        plaintext, prefix12, key_hash = _generate_key()
        new_key = APIKey(
            user_id=user.id,
            key_prefix=prefix12,
            key_hash=key_hash,
            name=FIRST_KEY_LABEL,
            label=FIRST_KEY_LABEL,
            is_active=True,
        )
        db.add(new_key)
        db.commit()
        db.refresh(new_key)

        logger.info(
            "Auto-minted first API key %s for user %s (flywheel F1.2, tier=%s)",
            new_key.id,
            user.id,
            tier,
        )
        new_key.plaintext = plaintext  # type: ignore[attr-defined]  # transient only
        return new_key
    # Rationale: key auto-mint must never block OAuth sign-in; log and continue.
    except Exception:  # noqa: BLE001
        logger.exception("Auto-mint of first API key failed for user %s (non-fatal)", user.id)
        try:
            db.rollback()
        # Rationale: rollback itself must never raise past this boundary.
        except Exception:  # noqa: BLE001
            pass
        return None
