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
    provenance_id: str | None = None,
) -> dict:
    """Report that an installed recipe is broken, has wrong instructions,
    or fails on this host.

    Use when the user says 'this skill is broken', 'report this skill',
    or when an install/run fails. Auto-creates a labelled GitHub issue
    with the failure signature.

    spotify_0608 Ph E: when ``provenance_id`` (returned by any install transport)
    is supplied, the issue routes DETERMINISTICALLY to the curator repo of the
    cookbook the skill was installed from, instead of always going to the
    platform default repo.
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

    # spotify_0608 Ph E — provenance-routed delivery. When a provenance_id maps
    # to a cookbook with a configured curator repo, file the issue THERE (PAT
    # path) so the creator who curated the cookbook gets "how agents used your
    # skill + what broke" in their own repo. Falls back to the default
    # dispatch_event pipeline when no provenance / no custom routing.
    if provenance_id:
        try:
            from app.services.provenance import route_targets_for_provenance

            targets = route_targets_for_provenance(db, provenance_id)
        # Rationale: a provenance-resolution hiccup must not drop the report —
        # fall through to the default dispatch below.
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill-error: provenance routing raised: %s", exc)
            targets = []
        for t in targets:
            if t.mode == "pat" and t.pat_enc:
                try:
                    from app.feedback_cred_vault import decrypt_pat
                    from app.feedback_github import create_issue

                    token = decrypt_pat(t.pat_enc)
                    title = f"[skill-error] {slug}: {summary[:80]}"
                    body_md = (
                        f"**Skill:** `{slug}`\n\n"
                        f"**Summary:** {summary}\n\n"
                        f"**Error signature:** `{signature.lower()}`\n"
                        f"**Report ID:** {report.id}\n"
                    )
                    if details:
                        body_md += f"\n**Details:**\n```\n{details[:1500]}\n```\n"
                    url = create_issue(
                        t.repo, token, title=title, body=body_md, labels=["skill-error", "recipes"]
                    )
                    if url:
                        logger.info("skill-error: routed to curator repo=%s url=%s", t.repo, url)
                        return {
                            "ok": True,
                            "id": str(report.id),
                            "issue_url": url,
                            "status": "filed",
                            "accepted": True,
                            "anonymized": True,
                            "deduped": False,
                            "routed_to": "curator",
                        }
                # Rationale: curator-repo dispatch failure → fall back to default.
                except Exception as exc:  # noqa: BLE001
                    logger.warning("skill-error: curator dispatch failed repo=%s: %s", t.repo, exc)

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
