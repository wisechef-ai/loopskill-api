"""Phase C — POST /api/v1/skill-error endpoint.

Receives skill error reports from the `recipes report-error` CLI. Each payload
is anonymized via Presidio + custom WiseChef layer, then persisted. Opt-in only:
callers MUST set RECIPES_REPORT_ERRORS=true env var — endpoint returns 403 otherwise.

Reuses the existing IncidentReport model (incident_reports table) and rate-limiting
from feedback_routes. The difference from /api/feedback/incident is:
  1. Presidio-based PII anonymization (not just regex audit)
  2. Custom WiseChef layer for internal names/infra
  3. skill_slug-based lookup (CLI sends slug, not UUID)
  4. Opt-in enforcement
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import IncidentReport, Skill

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["skill-errors"])


# ── Opt-in check ────────────────────────────────────────────────────────

def _is_opted_in() -> bool:
    """Check if error reporting is enabled via env var. Default OFF."""
    return os.environ.get("RECIPES_REPORT_ERRORS", "").lower() == "true"


# ── Presidio anonymization ──────────────────────────────────────────────

try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    _analyzer = AnalyzerEngine()
    _PRESIDIO_AVAILABLE = True
except ImportError:
    _analyzer = None
    _PRESIDIO_AVAILABLE = False
    logger.warning("presidio-analyzer not installed; falling back to regex-only anonymization")


# Custom WiseChess PII patterns — internal names/infra that Presidio won't catch
_WISECHEF_NAMES = {
    "adam", "bombilla", "marco", "karol", "olek", "mariusz",
    "tori", "wise", "chef",  # agent names
    "adam krawczyk", "artur krawczyk",
}

_WISECHEF_INFRA_PATTERNS = [
    re.compile(r"/home/\w+/", re.IGNORECASE),
    re.compile(r"wisechef-\w+", re.IGNORECASE),
    re.compile(r"wisechef\.ai", re.IGNORECASE),
    re.compile(r"obsidian-vault", re.IGNORECASE),
    re.compile(r"paperclip", re.IGNORECASE),
    re.compile(r"cognee", re.IGNORECASE),
    re.compile(r"192\.168\.\d+\.\d+"),  # internal IPs
    re.compile(r"10\.0\.\d+\.\d+"),
    re.compile(r"(?:77\.42\.92\.141|168\.119\.57\.68|178\.104\.19\.6|89\.167\.102\.128)"),  # fleet IPs
    re.compile(r"adkrawcz", re.IGNORECASE),
    re.compile(r"khasreto", re.IGNORECASE),
]

_REDACTED = "[REDACTED]"


def _anonymize_text(text: str) -> str:
    """Run Presidio + custom WiseChef anonymization on a string."""
    if not text:
        return text

    result = text

    # Phase 1: Presidio (if available)
    if _PRESIDIO_AVAILABLE and _analyzer:
        try:
            results = _analyzer.analyze(text=text, language="en")
            # Replace from end to preserve indices
            for r in sorted(results, key=lambda x: x.start, reverse=True):
                result = result[:r.start] + _REDACTED + result[r.end:]
        except Exception as e:
            logger.warning("Presidio analysis failed: %s", e)

    # Phase 2: Custom WiseChef patterns
    for pattern in _WISECHEF_INFRA_PATTERNS:
        result = pattern.sub(_REDACTED, result)

    # Phase 3: Name scrubbing (word-boundary matching)
    for name in _WISECHEF_NAMES:
        result = re.sub(r'\b' + re.escape(name) + r'\b', _REDACTED, result, flags=re.IGNORECASE)

    return result


def _anonymize_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Anonymize all string fields in a payload."""
    result = {}
    for k, v in data.items():
        if isinstance(v, str):
            result[k] = _anonymize_text(v)
        elif isinstance(v, dict):
            result[k] = {kk: _anonymize_text(str(vv)) if isinstance(vv, str) else vv
                         for kk, vv in v.items()}
        else:
            result[k] = v
    return result


# ── Regex audit (kept from feedback_routes for defense-in-depth) ────────

_FORBIDDEN_PATTERN = re.compile(
    r"("
    r"rec_[0-9a-f]{16,}"
    r"|api[_-]?key"
    r"|secret"
    r"|password"
    r"|bearer\s"
    r"|/home/"
    r"|/Users/"
    r")",
    re.IGNORECASE,
)


def _audit_payload(payload: dict[str, Any]) -> str | None:
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


# ── Rate limiting ───────────────────────────────────────────────────────

_RATE_LIMIT_MAX = 20  # 20/hr for skill-error (higher than incident's 10)
_RATE_LIMIT_WINDOW_S = 3600
_buckets: dict[str, list[float]] = defaultdict(list)
_buckets_lock = threading.Lock()


def _check_rate_limit(agent_fp_anon: str) -> bool:
    now = time.monotonic()
    with _buckets_lock:
        bucket = _buckets[agent_fp_anon]
        cutoff = now - _RATE_LIMIT_WINDOW_S
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= _RATE_LIMIT_MAX:
            return False
        bucket.append(now)
        return True


# ── Schemas ─────────────────────────────────────────────────────────────

_HEX_RE = re.compile(r"^[0-9a-f]+$")


class SkillErrorIn(BaseModel):
    """Payload for skill error reports. Uses skill_slug for CLI convenience."""
    skill_slug: str = Field(min_length=1, max_length=128)
    error_signature: str = Field(min_length=8, max_length=128)
    env_fingerprint: dict[str, Any] = {}
    agent_fp_anon: str = Field(min_length=8, max_length=128)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
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
        if len(v) > 32:
            raise ValueError("env_fingerprint has too many keys")
        for key, val in v.items():
            if not isinstance(key, str) or len(key) > 64:
                raise ValueError("env_fingerprint key invalid")
            if isinstance(val, (dict, list)):
                raise ValueError("env_fingerprint values must be scalar")
        return v


class SkillErrorOut(BaseModel):
    id: UUID
    accepted: bool = True
    anonymized: bool = True


# ── Endpoint ────────────────────────────────────────────────────────────

@router.post(
    "/skill-error",
    response_model=SkillErrorOut,
    status_code=status.HTTP_201_CREATED,
)
def post_skill_error(
    payload: SkillErrorIn,
    db: Session = Depends(get_db),
) -> SkillErrorOut:
    # Opt-in check
    if not _is_opted_in():
        raise HTTPException(
            status_code=403,
            detail="Error reporting is not enabled. Set RECIPES_REPORT_ERRORS=true to opt in.",
        )

    # Look up skill by slug
    skill = db.query(Skill).filter(Skill.slug == payload.skill_slug).first()
    if skill is None:
        raise HTTPException(status_code=404, detail=f"skill not found: {payload.skill_slug}")

    # Build raw dict for anonymization
    raw = {
        "command": payload.command or "",
        "stack_trace_top": payload.stack_trace_top or "",
        "env_fingerprint": {k: str(v) for k, v in payload.env_fingerprint.items()},
    }

    # Defense-in-depth: regex audit BEFORE anonymization (catch what Presidio misses)
    hit = _audit_payload(raw)
    if hit:
        raise HTTPException(
            status_code=400,
            detail=f"payload rejected by audit: PII detected — {hit}",
        )

    # Anonymize via Presidio + custom layer
    anon = _anonymize_payload(raw)

    # Rate limit
    if not _check_rate_limit(payload.agent_fp_anon):
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded for agent_fp_anon",
        )

    occurred = payload.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)

    report = IncidentReport(
        skill_id=skill.id,
        error_signature=payload.error_signature,
        env_fingerprint=anon.get("env_fingerprint", payload.env_fingerprint),
        agent_fp_anon=payload.agent_fp_anon,
        occurred_at=occurred,
        command=anon.get("command", payload.command),
        exit_code=payload.exit_code,
        stack_trace_top=anon.get("stack_trace_top", payload.stack_trace_top),
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    logger.info(
        "skill-error accepted: skill=%s sig=%s agent=%s",
        payload.skill_slug,
        payload.error_signature[:12],
        payload.agent_fp_anon[:12],
    )

    return SkillErrorOut(id=report.id, accepted=True, anonymized=True)


@router.get("/skill-error/health")
def skill_error_health():
    """Health check for the error reporting subsystem."""
    return {
        "status": "ok",
        "opted_in": _is_opted_in(),
        "presidio_available": _PRESIDIO_AVAILABLE,
    }
