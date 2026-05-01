"""B.2 — `/api/feedback/incident` endpoint.

Receives anonymous failure reports from `recipes-auto-improve`. Each payload
is regex-audited at the wire to scrub creds/paths before persistence, then
rate-limited per `agent_fp_anon` (10 reports/hour, in-process token bucket).

The handler is intentionally permissive about callers — it only validates
shape + content. Identity is the agent_fp_anon hash; we never tie a report
to a user account.
"""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import IncidentReport, Skill


router = APIRouter(prefix="/api/feedback", tags=["feedback"])


# ── Regex audit ─────────────────────────────────────────────────────────
# These patterns reject any payload that contains creds, absolute home
# paths, or other identifying material. Order matters only for clarity.

_FORBIDDEN_PATTERN = re.compile(
    r"("
    r"rec_[0-9a-f]{16,}"          # API keys
    r"|api[_-]?key"                # any "api_key" / "api-key" mention
    r"|secret"
    r"|password"
    r"|bearer\s"                   # auth headers
    r"|/home/"                     # *nix home paths
    r"|/Users/"                    # macOS home paths
    r")",
    re.IGNORECASE,
)


def audit_payload(payload: dict[str, Any]) -> str | None:
    """Walk all string fields. Return the first forbidden hit, or None."""
    for k, v in payload.items():
        if isinstance(v, str):
            m = _FORBIDDEN_PATTERN.search(v)
            if m:
                return f"{k}: {m.group(1)}"
        elif isinstance(v, dict):
            for kk, vv in v.items():
                if isinstance(vv, str) and _FORBIDDEN_PATTERN.search(vv):
                    return f"{k}.{kk}"
    return None


# ── Rate limiting (in-process token bucket per agent_fp_anon) ───────────
# 10 requests / hour. Sliding window via deque of timestamps. Process-local
# only; production deployment should swap this for a Redis backend.

_RATE_LIMIT_MAX = 10
_RATE_LIMIT_WINDOW_S = 3600

_buckets: dict[str, list[float]] = defaultdict(list)
_buckets_lock = threading.Lock()


def _check_rate_limit(agent_fp_anon: str, now: float | None = None) -> bool:
    now = now if now is not None else time.monotonic()
    with _buckets_lock:
        bucket = _buckets[agent_fp_anon]
        cutoff = now - _RATE_LIMIT_WINDOW_S
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= _RATE_LIMIT_MAX:
            return False
        bucket.append(now)
        return True


def _reset_rate_limits() -> None:
    with _buckets_lock:
        _buckets.clear()


# ── Schemas ─────────────────────────────────────────────────────────────

_HEX_RE = re.compile(r"^[0-9a-f]+$")


class IncidentIn(BaseModel):
    skill_id: UUID
    error_signature: str = Field(min_length=8, max_length=128)
    env_fingerprint: dict[str, Any]
    agent_fp_anon: str = Field(min_length=8, max_length=128)
    occurred_at: datetime
    command: str | None = Field(default=None, max_length=2048)
    exit_code: int | None = None
    stack_trace_top: str | None = Field(default=None, max_length=2048)

    @field_validator("error_signature")
    @classmethod
    def _hex_signature(cls, v: str) -> str:
        if not _HEX_RE.match(v.lower()):
            raise ValueError("error_signature must be hex")
        return v.lower()

    @field_validator("env_fingerprint")
    @classmethod
    def _env_fp_shape(cls, v: dict[str, Any]) -> dict[str, Any]:
        # Don't enforce specific keys, but cap depth/size to a sane shape.
        if len(v) > 32:
            raise ValueError("env_fingerprint has too many keys")
        for key, val in v.items():
            if not isinstance(key, str) or len(key) > 64:
                raise ValueError("env_fingerprint key invalid")
            if isinstance(val, (dict, list)):
                raise ValueError("env_fingerprint values must be scalar")
        return v


class IncidentOut(BaseModel):
    id: UUID
    accepted: bool = True


# ── Endpoint ────────────────────────────────────────────────────────────

@router.post(
    "/incident",
    response_model=IncidentOut,
    status_code=status.HTTP_201_CREATED,
)
def post_incident(
    payload: IncidentIn,
    db: Session = Depends(get_db),
) -> IncidentOut:
    raw = payload.model_dump(mode="python")
    # The audit walks string fields; UUID/datetime/int are serialized to str
    # for the regex check below.
    flat: dict[str, Any] = {
        "error_signature": payload.error_signature,
        "agent_fp_anon": payload.agent_fp_anon,
        "command": payload.command or "",
        "stack_trace_top": payload.stack_trace_top or "",
        "env_fingerprint": {
            k: str(v) for k, v in payload.env_fingerprint.items()
        },
    }
    hit = audit_payload(flat)
    if hit:
        raise HTTPException(
            status_code=400,
            detail=f"payload rejected by regex audit: {hit}",
        )

    if not _check_rate_limit(payload.agent_fp_anon):
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded for agent_fp_anon",
        )

    # FK check — skill must exist. Cheap pre-check beats raising IntegrityError.
    skill = db.query(Skill).filter(Skill.id == payload.skill_id).first()
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")

    occurred = payload.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)

    report = IncidentReport(
        skill_id=payload.skill_id,
        error_signature=payload.error_signature,
        env_fingerprint=payload.env_fingerprint,
        agent_fp_anon=payload.agent_fp_anon,
        occurred_at=occurred,
        command=payload.command,
        exit_code=payload.exit_code,
        stack_trace_top=payload.stack_trace_top,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return IncidentOut(id=report.id, accepted=True)
