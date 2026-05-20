"""MCP tool: recipes_publish_request.

Submit a skill for review and potential public-catalog inclusion.
Mirrors the recipes_feedback / recipes_request_recipe pattern EXACTLY.

Internal flow:
  1. Validate slug (SLUG_RE) + semver (SEMVER_RE)
  2. Check feedback_ratelimit (1 publish-request per 24h per (user, slug))
  3. Build tarball in-memory; run scan_tarball + quality gate
     - HIGH findings → return {error:'quality_gate_failed'} WITHOUT opening issue
     - medium/low    → include in warnings
  4. Compute sha256(tarball)
  5. INSERT SkillPublishRequest row (status='pending')
  6. github_dispatch.dispatch_event('skill-publish-request', {row_id, slug, ...})
  7. Return {request_id, slug, status:'pending_review', issue_url, sha256, warnings:[]}
"""

from __future__ import annotations

import hashlib
import io
import logging
import tarfile as _tarfile
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app import feedback_ratelimit, github_dispatch
from app.models import SkillPublishRequest
from app.publisher_routes import SEMVER_RE, SLUG_RE
from app.security_scan import scan_tarball

logger = logging.getLogger(__name__)

# 10 MB cap — same as publisher_routes
_MAX_TARBALL_BYTES = 10 * 1024 * 1024

# Tool name used for the rate-limiter bucket
_TOOL_KEY = "skill-publish-request"


def _gate_scan(tarball_bytes: bytes) -> list[dict]:
    """Wrapper around app.skill_quality_gate.scan_tarball_bytes.

    Returns a list of finding dicts (with 'severity' key).
    Returns [] if the module is not importable (fail open with a log).
    """
    try:
        from app.skill_quality_gate import scan_tarball_bytes

        return [f if isinstance(f, dict) else f.to_dict() for f in scan_tarball_bytes(tarball_bytes)]
    except ImportError:
        logger.warning("skill_quality_gate not importable; skipping gate scan in publish_request")
        return []


def _build_tarball(
    slug: str,
    content: str,
    version: str,
    description: str | None,
    tier: str,
    license: str,
    references: list[dict[str, str]] | None,
    scripts: list[dict[str, str]] | None,
    changelog: str | None,
) -> bytes:
    """Build a minimal .tar.gz in-memory from the provided fields."""
    buf = io.BytesIO()
    with _tarfile.open(fileobj=buf, mode="w:gz") as t:
        # Add SKILL.md
        md_bytes = content.encode("utf-8")
        ti = _tarfile.TarInfo(name="SKILL.md")
        ti.size = len(md_bytes)
        t.addfile(ti, io.BytesIO(md_bytes))

        # Add skill.toml
        desc_escaped = (description or "").replace('"', '\\"')
        toml_lines = [
            "[skill]",
            f'name = "{slug}"',
            f'version = "{version}"',
            f'description = "{desc_escaped}"',
            f'license = "{license}"',
            'entrypoint = "SKILL.md"',
            f'tier = "{tier}"',
        ]
        if changelog:
            toml_lines.append(f'changelog = "{changelog.replace(chr(34), chr(92)+chr(34))}"')
        toml_bytes = ("\n".join(toml_lines) + "\n").encode("utf-8")
        ti2 = _tarfile.TarInfo(name="skill.toml")
        ti2.size = len(toml_bytes)
        t.addfile(ti2, io.BytesIO(toml_bytes))

        # Add optional references
        for ref in references or []:
            ref_path = ref.get("path", f"references/{uuid4().hex[:8]}.md")
            ref_content = ref.get("content", "").encode("utf-8")
            ti_ref = _tarfile.TarInfo(name=ref_path)
            ti_ref.size = len(ref_content)
            t.addfile(ti_ref, io.BytesIO(ref_content))

        # Add optional scripts
        for script in scripts or []:
            scr_path = script.get("path", f"scripts/{uuid4().hex[:8]}.sh")
            scr_content = script.get("content", "").encode("utf-8")
            ti_scr = _tarfile.TarInfo(name=scr_path)
            ti_scr.size = len(scr_content)
            t.addfile(ti_scr, io.BytesIO(scr_content))

    return buf.getvalue()


def recipes_publish_request(
    db: Session,
    *,
    slug: str,
    content: str,
    version: str = "1.0.0",
    description: str | None = None,
    tier: str = "pro",
    is_public: bool = True,
    references: list[dict[str, str]] | None = None,
    scripts: list[dict[str, str]] | None = None,
    license: str = "MIT",
    changelog: str | None = None,
    force: bool = False,
    confirmation: str | None = None,
    api_key_id: str | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Submit a skill for review and potential public-catalog inclusion.

    Validates the skill quality gates locally (scan_tarball + quality gate)
    before opening a GitHub issue. HIGH-severity findings block submission.
    Medium/low findings are returned as warnings.

    Rate limited to 1 publish-request per 24h per (identity, slug).
    Use force=True + confirmation='...' to override the rate limit.
    """
    # Public-scope MCP tool: rate-limited skill publish request; no private data exposed.
    # Validates quality gates locally before opening a GitHub issue for human review.

    # ── 1. Validate slug ──────────────────────────────────────────────────
    if not slug or not SLUG_RE.match(slug):
        return {
            "error": "invalid_slug",
            "detail": (f"Slug {slug!r} does not match ^[a-z0-9][a-z0-9_-]{{0,63}}$"),
        }

    # ── 2. Validate semver ────────────────────────────────────────────────
    if not version or not SEMVER_RE.match(version):
        return {
            "error": "invalid_version",
            "detail": f"Version {version!r} does not match semver (N.N.N)",
        }

    # ── 3. Rate limit (1 publish-request per 24h per identity+slug) ───────
    identity = f"api_key:{api_key_id}" if api_key_id else "anon"
    # Incorporate slug into the signature so the limit is per (identity, slug)
    sig = hashlib.sha256(f"{identity}|{slug}|{version}".encode()).hexdigest()

    rl = feedback_ratelimit.check_and_record(
        identity=identity,
        tool=_TOOL_KEY,
        signature=sig,
        force=force,
        confirmation=confirmation,
    )

    if not rl.allowed:
        if rl.deduped:
            return {
                "status": "pending_review",
                "slug": slug,
                "request_id": "",
                "issue_url": rl.issue_url,
                "sha256": "",
                "warnings": [],
                "deduped": True,
            }
        if rl.loop_block:
            return {
                "error": "loop_detector_cooldown",
                "retry_at": rl.retry_at.isoformat() if rl.retry_at else None,
                "force_available": False,
            }
        return {
            "error": "rate_limit_exceeded",
            "force_available": rl.force_available,
            "last_submissions": rl.last_submissions,
        }

    # ── 4. Build tarball in-memory ────────────────────────────────────────
    tarball_bytes = _build_tarball(
        slug=slug,
        content=content,
        version=version,
        description=description,
        tier=tier,
        license=license,
        references=references,
        scripts=scripts,
        changelog=changelog,
    )

    if len(tarball_bytes) > _MAX_TARBALL_BYTES:
        return {
            "error": "tarball_too_large",
            "detail": f"Tarball exceeds 10 MB limit ({len(tarball_bytes)} bytes)",
        }

    # ── 5. Security scan (scan_tarball) ───────────────────────────────────
    # Build a minimal skill_section dict for scan_tarball
    skill_section = {
        "name": slug,
        "version": version,
        "description": description or "",
        "license": license,
        "entrypoint": "SKILL.md",
        "tier": tier,
    }
    scan_findings = scan_tarball(tarball_bytes, skill_section)
    high_findings = [f for f in scan_findings if f.severity in ("high", "critical")]
    if high_findings:
        return {
            "error": "quality_gate_failed",
            "findings": [
                {
                    "class": f.pattern_class,
                    "file": f.file_path,
                    "line": f.line_no,
                    "snippet": f.snippet[:200],
                    "why": f.rationale,
                    "severity": f.severity,
                }
                for f in high_findings
            ],
        }

    # Medium/low from scan_tarball become warnings
    warnings: list[dict] = [
        {
            "class": f.pattern_class,
            "file": f.file_path,
            "line": f.line_no,
            "snippet": f.snippet[:200],
            "why": f.rationale,
            "severity": f.severity,
        }
        for f in scan_findings
        if f.severity in ("medium", "low")
    ]

    # ── 6. Quality gate (skill_quality_gate) ─────────────────────────────
    gate_findings = _gate_scan(tarball_bytes)
    gate_blocks = [f for f in gate_findings if f.get("severity") == "block"]
    if gate_blocks:
        return {
            "error": "quality_gate_failed",
            "findings": gate_blocks[:25],
        }
    # Non-blocking gate findings → warnings
    warnings.extend([{**f, "source": "quality_gate"} for f in gate_findings if f.get("severity") == "warn"])

    # ── 7. Compute sha256 ─────────────────────────────────────────────────
    sha256_hex = hashlib.sha256(tarball_bytes).hexdigest()

    # ── 8. INSERT SkillPublishRequest row ─────────────────────────────────
    row = SkillPublishRequest(
        id=uuid4(),
        slug=slug,
        version=version,
        sha256=sha256_hex,
        tarball_bytes=tarball_bytes,
        status="pending",
        issue_url="",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    row_id = str(row.id)

    # Content preview (first 200 lines of SKILL.md)
    preview_lines = content.splitlines()[:200]
    content_preview = "\n".join(preview_lines)

    # ── 9. Dispatch GitHub event ──────────────────────────────────────────
    gh_url = (
        github_dispatch.dispatch_event(
            "skill-publish-request",
            {
                "row_id": row_id,
                "slug": slug,
                "version": version,
                "sha256": sha256_hex,
                "tier": tier,
                "is_public": is_public,
                "description": description or "",
                "content_preview": content_preview,
                "warnings": warnings,
                "license": license,
            },
        )
        or ""
    )

    if gh_url:
        row.issue_url = gh_url
        db.commit()
        feedback_ratelimit.update_dedup_url(sig, gh_url)

    return {
        "request_id": row_id,
        "slug": slug,
        "status": "pending_review",
        "issue_url": gh_url,
        "sha256": sha256_hex,
        "warnings": warnings,
    }
