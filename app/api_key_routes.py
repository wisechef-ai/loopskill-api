"""API key management routes — generate, list, revoke.

Phase C (top1pct_1105):
- Multi-key support with tier cap enforcement (Free/Pro = 1, Pro+ = 20)
- Per-cookbook scoping: optional cookbook_id on create
- Human label: optional label field (persisted as both `name` and `label`)
- GET /api-keys returns install_count_total + install_count_7d per key
- REMOVED: "revoke-before-create" one-per-user policy → replaced by cap check

Original WIS-640: users need a `rec_*` key to use the meta-skill or any /api/* route.
Key format: rec_live_<32 random urlsafe chars>
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth_routes import get_current_user_optional
from app.database import get_db
from app.models import APIKey, Bundle, InstallEvent, User

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["api-keys"])


KEY_PREFIX = "rec_live_"
KEY_BODY_LEN = 32  # urlsafe chars after prefix

# Tier → max active keys cap
KEY_CAP: dict[str, int] = {
    "free": 1,
    "pro": 1,
    # Legacy aliases — sunset 2026-06-10
    "cook": 1,  # legacy alias → pro
    "pro_plus": 20,
    "operator": 20,  # legacy alias → pro_plus
    "studio": 20,  # legacy alias → pro_plus
}
DEFAULT_CAP = 1  # fallback for unknown/null tiers


def _generate_key() -> tuple[str, str, str]:
    """Generate a fresh API key. Returns (plaintext, prefix12, sha256_hash)."""
    body = secrets.token_urlsafe(KEY_BODY_LEN)
    plaintext = f"{KEY_PREFIX}{body}"
    prefix12 = plaintext[:12]
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, prefix12, key_hash


def _require_user(user: User | None) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="login_required")
    return user


# ── Pydantic schemas ──────────────────────────────────────────────────────


class CreateKeyIn(BaseModel):
    label: str | None = None  # human label ≤100 chars
    cookbook_id: str | None = None  # UUID of an owned cookbook
    name: str | None = None  # legacy alias for label


# ── Install count aggregation helper ─────────────────────────────────────


def _fetch_install_counts(db: Session, key_ids: list[UUID]) -> dict[UUID, dict]:
    """Return {key_id: {total: int, last_7d: int}} for a list of key IDs.

    Uses SQLAlchemy ORM aggregation — handles UUID/binary correctly across
    SQLite (tests) and Postgres (prod) without raw SQL type coercion issues.
    """
    if not key_ids:
        return {}

    result: dict[UUID, dict] = {kid: {"total": 0, "last_7d": 0} for kid in key_ids}

    from datetime import timedelta

    from sqlalchemy import case
    from sqlalchemy import func as sqlfunc

    cutoff = datetime.now(UTC) - timedelta(days=7)

    try:
        rows = (
            db.query(
                InstallEvent.api_key_id,
                sqlfunc.count().label("total"),
                sqlfunc.sum(case((InstallEvent.created_at >= cutoff, 1), else_=0)).label("last_7d"),
            )
            .filter(InstallEvent.api_key_id.in_(key_ids))
            .group_by(InstallEvent.api_key_id)
            .all()
        )

        for row in rows:
            kid = row[0]
            if kid is None:
                continue
            if not isinstance(kid, UUID):
                try:
                    kid = UUID(str(kid))
                except (ValueError, AttributeError):
                    continue
            if kid in result:
                result[kid] = {
                    "total": int(row[1] or 0),
                    "last_7d": int(row[2] or 0),
                }
    # Rationale: install-count aggregation is non-critical; any DB error → log and return partial
    except Exception as exc:  # noqa: BLE001
        logger.warning("install_count aggregation failed: %s", exc)

    return result


# ── Routes ────────────────────────────────────────────────────────────────


@router.post("/api-keys")
async def create_api_key(
    request: Request,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Create a new API key for the authenticated user.

    Phase C policy:
    - Free / Pro (legacy cook)  : max 1 active key
    - Pro+ (legacy operator)    : max 20 active keys
    - Optional cookbook_id: must belong to the calling user
    - Optional label: human-readable name ≤100 chars

    The plaintext key is returned ONCE — store it.
    """
    user = _require_user(user)

    # Parse body (best-effort; not all callers send JSON)
    body: dict = {}
    try:
        body = await request.json()
    # Rationale: request body is optional for key creation; malformed JSON → use defaults
    except Exception:  # noqa: BLE001
        pass
    if not isinstance(body, dict):
        body = {}

    # ── Tier cap enforcement ──────────────────────────────────────────────
    tier = user.subscription_tier or "free"
    cap = KEY_CAP.get(tier, DEFAULT_CAP)

    active_count = (
        db.query(APIKey)
        .filter(APIKey.user_id == user.id, APIKey.is_active == True)  # noqa: E712
        .count()
    )
    if active_count >= cap:
        raise HTTPException(
            status_code=403,
            detail=f"key_cap_exceeded — max {cap} active key(s) on {tier} tier",
        )

    # ── Optional bundle scoping ─────────────────────────────────────────
    cookbook_id: UUID | None = None
    if body.get("cookbook_id"):
        try:
            cookbook_id = UUID(str(body["cookbook_id"]))
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid_cookbook_id")

        cb = (
            db.query(Bundle)  # compat-alias
            .filter(Bundle.id == cookbook_id, Bundle.bundle_owner == user.id)  # compat-alias
            .first()
        )
        if not cb:
            raise HTTPException(status_code=404, detail="cookbook_not_found")

    # ── Label (prefer explicit `label`, fall back to `name`) ─────────────
    raw_label: str | None = body.get("label") or body.get("name")
    if raw_label and len(raw_label) > 100:
        raw_label = raw_label[:100]
    label = raw_label or "default"

    # ── Create key ────────────────────────────────────────────────────────
    plaintext, prefix12, key_hash = _generate_key()

    new_key = APIKey(
        user_id=user.id,
        key_prefix=prefix12,
        key_hash=key_hash,
        name=label,  # keep `name` populated for backwards-compat reads
        label=label,
        bundle_id=cookbook_id,  # compat-alias
        is_active=True,
    )
    db.add(new_key)
    db.commit()
    db.refresh(new_key)

    logger.info(
        "Created API key %s for user %s (tier=%s cap=%d active=%d cookbook=%s)",
        new_key.id,
        user.id,
        tier,
        cap,
        active_count + 1,
        str(cookbook_id) if cookbook_id else "none",
    )

    # Plaintext returned ONCE — never again
    return {
        "id": str(new_key.id),
        "key": plaintext,
        "prefix": prefix12,
        "label": new_key.label,
        "name": new_key.name,
        "bundle_id": str(new_key.bundle_id) if new_key.bundle_id else None,
        "created_at": new_key.created_at.isoformat() if new_key.created_at else None,
        "warning": "Save this key now — it will not be shown again.",
    }


@router.get("/api-keys")
async def list_api_keys(
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """List the authenticated user's API keys (no plaintext).

    Phase C additions: each key item now includes:
      - cookbook_id (UUID or null)
      - label (human label)
      - install_count_total (all-time installs via this key)
      - install_count_7d   (installs in the last 7 days)
    """
    user = _require_user(user)
    keys = db.query(APIKey).filter(APIKey.user_id == user.id).order_by(APIKey.created_at.desc()).all()

    # Aggregate install counts in one query
    key_ids = [k.id for k in keys if k.id is not None]
    counts = _fetch_install_counts(db, key_ids)

    return {
        "keys": [
            {
                "id": str(k.id),
                "prefix": k.key_prefix,
                "label": k.label or k.name,
                "name": k.name,
                "bundle_id": str(k.bundle_id) if k.bundle_id else None,
                "is_active": k.is_active,
                "created_at": k.created_at.isoformat() if k.created_at else None,
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                "install_count_total": counts.get(k.id, {}).get("total", 0),
                "install_count_7d": counts.get(k.id, {}).get("last_7d", 0),
            }
            for k in keys
        ],
    }


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
):
    """Revoke an API key. Idempotent — already-revoked or missing keys return 204."""
    user = _require_user(user)
    try:
        key_uuid = UUID(key_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="invalid_key_id")

    key = db.query(APIKey).filter(APIKey.id == key_uuid, APIKey.user_id == user.id).first()
    if key and key.is_active:
        key.is_active = False
        db.commit()
        logger.info("Revoked API key %s for user %s", key.id, user.id)

    return {"revoked": True, "id": key_id}
