"""MCP tool: recipes_report_skill_error.

Report that an installed recipe is broken, has wrong instructions, or fails
on this host. Wraps the same helpers as POST /api/v1/skill-error without
making an HTTP round-trip.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app import feedback_ratelimit, github_dispatch
from app.models import IncidentReport, Skill

logger = logging.getLogger(__name__)

_HEX_RE = re.compile(r"^[0-9a-f]+$")


def _sha256(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _is_opted_in() -> bool:
    return os.environ.get("RECIPES_REPORT_ERRORS", "").lower() == "true"


def recipes_report_skill_error(
    db: Session,
    *,
    slug: str,
    signature: str,
    summary: str,
    details: str | None = None,
    agent_id: str | None = None,
    api_key_id: str | None = None,
) -> dict:
    """Report that an installed recipe is broken, has wrong instructions,
    or fails on this host.

    Use when the user says 'this skill is broken', 'report this skill',
    or when an install/run fails. Auto-creates a labelled GitHub issue
    with the failure signature.
    """
    # Public-scope MCP tool: rate-limited error report submission; no private data exposed.
    if not _is_opted_in():
        return {
            "ok": False,
            "error": "Error reporting is not enabled. Set RECIPES_REPORT_ERRORS=true to opt in.",
        }

    if not slug or len(slug) > 128:
        return {"ok": False, "error": "slug must be 1-128 characters"}

    # signature must be hex
    if not signature or not _HEX_RE.match(signature.lower()):
        return {"ok": False, "error": "signature must be a hex string"}

    skill = db.query(Skill).filter(Skill.slug == slug).first()
    if skill is None:
        return {"ok": False, "error": f"skill not found: {slug}"}

    identity = f"api_key:{api_key_id}" if api_key_id else (f"agent:{agent_id}" if agent_id else "unknown")

    # Compute composite signature for dedup/ratelimit
    composite_sig = _sha256(slug, signature.lower())

    rl = feedback_ratelimit.check_and_record(
        identity=identity,
        tool="skill-error",
        signature=composite_sig,
    )

    if not rl.allowed:
        if rl.deduped:
            return {
                "ok": True,
                "id": "",
                "issue_url": rl.issue_url,
                "deduped": True,
                "accepted": True,
                "anonymized": True,
            }
        return {
            "ok": False,
            "error": "rate_limit_exceeded",
            "force_available": rl.force_available,
        }

    # Also check the skill-error specific backstop
    if not feedback_ratelimit.check_skill_error_backstop(identity):
        return {"ok": False, "error": "skill_error rate limit exceeded (30/hr)"}

    report = IncidentReport(
        skill_id=skill.id,
        error_signature=signature.lower(),
        env_fingerprint={},
        agent_fp_anon=agent_id or "mcp-tool",
        occurred_at=datetime.now(UTC),
        command=None,
        exit_code=None,
        stack_trace_top=details,
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    # dispatch_event now returns True on success (workflow PATCHes the real
    # issue URL back via /api/internal/feedback/{id}/issue-url) or None on
    # failure. The issue_url is therefore "pending" at submit time — clients
    # poll GET /api/feedback/{id} for the resolved URL.
    dispatched = github_dispatch.dispatch_event(
        "skill-error",
        {
            "id": str(report.id),
            "skill_slug": slug,
            "error_signature": signature.lower(),
            "agent_fp_anon": agent_id or "mcp-tool",
            "signature": composite_sig,
        },
    )

    return {
        "ok": True,
        "id": str(report.id),
        "issue_url": "",
        "status": "pending" if dispatched else "failed",
        "accepted": True,
        "anonymized": True,
        "deduped": False,
    }
