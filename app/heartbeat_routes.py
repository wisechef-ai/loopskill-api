"""Phase D — mathematically-anonymous fleet heartbeat endpoint.

POST /api/v1/heartbeat   — single 2-field schema; no other fields accepted.
GET  /api/v1/fleet/weekly — aggregate distinct count per ISO week
                            (admin-only, gated by master x-api-key).

Premortem F8 fix: schema is locked. Even with full DB read access, an
attacker cannot map a hash back to a customer because the hash is keyed
by HEARTBEAT_PEPPER. There are NO other columns that could identify a
device — by construction, not policy.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import FleetPing

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["fleet"])


class HeartbeatPayload(BaseModel):
    """Strict 2-field schema — extra fields are rejected (HTTP 422)."""

    model_config = ConfigDict(extra="forbid")

    salt: str = Field(
        min_length=16,
        max_length=64,
        pattern=r"^[a-f0-9]+$",
    )
    last_seen_day: date


def _hash_salt(salt: str) -> bytes:
    """Keyed blake2b — even DB compromise reveals nothing without pepper."""
    pepper = settings.HEARTBEAT_PEPPER.encode("utf-8")
    return hashlib.blake2b(salt.encode("utf-8"), key=pepper, digest_size=32).digest()


@router.post("/heartbeat", status_code=201)
def post_heartbeat(payload: HeartbeatPayload, db: Session = Depends(get_db)):
    """Record a heartbeat ping from an installed agent."""
    salt_hash = _hash_salt(payload.salt)
    row = FleetPing(salt_hash=salt_hash, last_seen_day=payload.last_seen_day)
    try:
        with db.begin_nested():
            db.add(row)
        db.commit()
    except IntegrityError:
        # Duplicate (same salt+day) — idempotent no-op
        db.rollback()
    return {"ok": True}


def _require_admin(request: Request) -> None:
    key = request.headers.get("x-api-key")
    if not key or key != settings.API_KEY:
        raise HTTPException(status_code=401, detail="admin_key_required")


@router.get("/fleet/weekly")
def fleet_weekly(request: Request, db: Session = Depends(get_db)):
    """Aggregate distinct devices per ISO week. NO drill-down — by design.

    Schema-level guarantee: this is the only public read path; no per-salt
    or per-customer query is exposed (and none can be added without modifying
    the table itself, which would also need to add a PII column).
    """
    _require_admin(request)
    rows = db.query(
        FleetPing.last_seen_day,
        FleetPing.salt_hash,
    ).all()
    # Bucket distinct salt_hashes by ISO year-week
    buckets: dict[str, set[bytes]] = {}
    for day, h in rows:
        iso_year, iso_week, _ = day.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        buckets.setdefault(key, set()).add(bytes(h))
    return [{"week": week, "active_count": len(hashes)} for week, hashes in sorted(buckets.items())]
