"""Referral utility functions for LoopSkill.

Handles referral code generation, cookie extraction, and referral linking.
"""

import logging
import random
import string

from sqlalchemy.orm import Session

from app.models import Referral, User

logger = logging.getLogger(__name__)

BASE62_CHARS = string.ascii_letters + string.digits


def generate_referral_code(length: int = 8) -> str:
    """Generate a random base62 referral code."""
    return "".join(random.choices(BASE62_CHARS, k=length))


def ensure_referral_code(user: User, db: Session) -> str:
    """Ensure user has a referral_code, generating one if missing.

    Retries on collision (extremely unlikely with 62^8 = 218T keyspace).
    """
    if user.referral_code:
        return user.referral_code

    for _ in range(10):
        code = generate_referral_code()
        existing = db.query(User).filter(User.referral_code == code).first()
        if not existing:
            user.referral_code = code
            db.commit()
            db.refresh(user)
            return code

    raise RuntimeError("Failed to generate unique referral code after 10 attempts")


def process_referral_cookie(
    db: Session,
    new_user: User,
    ref_code: str | None,
) -> Referral | None:
    """Look up referrer from cookie code and create a Referral row.

    Called after successful user creation in OAuth callback.
    Returns the Referral if created, None otherwise.
    """
    if not ref_code:
        return None

    referrer = db.query(User).filter(User.referral_code == ref_code).first()
    if not referrer:
        logger.warning("Referral code %s not found — ignoring", ref_code)
        return None

    if referrer.id == new_user.id:
        return None  # no self-referrals

    # Check for existing referral (idempotent)
    existing = (
        db.query(Referral)
        .filter(
            Referral.referrer_user_id == referrer.id,
            Referral.referred_user_id == new_user.id,
        )
        .first()
    )
    if existing:
        return existing

    # Determine rate: first 50 referrers get 50%, rest get 30%
    referrer_rank = db.query(Referral).filter(Referral.referrer_user_id == referrer.id).count()
    from decimal import Decimal

    rate = Decimal("0.50") if referrer_rank < 50 else Decimal("0.30")

    referral = Referral(
        referrer_user_id=referrer.id,
        referred_user_id=new_user.id,
        referral_code=ref_code,
        referred_email=new_user.email,
        status="signed_up",
        rate=rate,
    )
    db.add(referral)

    # Set referred_by on user
    new_user.referred_by = referrer.id

    db.commit()
    db.refresh(referral)
    logger.info(
        "Created referral: referrer=%s referred=%s code=%s rate=%s",
        referrer.id,
        new_user.id,
        ref_code,
        rate,
    )
    return referral


# qa0208-w3 dual-accept: loopskill_ref is canonical (written going forward);
# recipes_ref is accepted as a read-only fallback so referral links minted
# before the rename still attribute correctly. See AGENTS.md "Legacy
# identifier deprecation windows" for the full dual-accept inventory.
REFERRAL_COOKIE_NAME = "loopskill_ref"
LEGACY_REFERRAL_COOKIE_NAME = "recipes_ref"  # compat-alias — read fallback only
REFERRAL_COOKIE_MAX_AGE = 2592000  # 30 days


def resolve_referral_cookie(request) -> str | None:
    """Read the referral code cookie, preferring the canonical name.

    Tries ``loopskill_ref`` first (canonical, written by ``_stamp_referral_cookie``);
    falls back to the legacy ``recipes_ref`` cookie so referral links minted
    before the qa0208-w3 rename still attribute correctly.
    """
    return request.cookies.get(REFERRAL_COOKIE_NAME) or request.cookies.get(LEGACY_REFERRAL_COOKIE_NAME)
