"""Sandbox API routes for WiseRecipes.

Endpoints:
  POST /api/skills/{slug}/sandbox/run   — execute a skill's sandbox entrypoint
  GET  /api/skills/{slug}/sandbox/status — check if a skill supports sandbox execution
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app import authz
from app.database import get_db
from app.models import Skill, SkillVersion, TelemetryEvent
from app.sandbox.profile import SandboxProfile
from app.sandbox.runner import SandboxRunner

router = APIRouter(prefix="/api", tags=["sandbox"])

# Shared runner instance (workspace can be configured via env)
SANDBOX_WORKSPACE = os.environ.get("WR_SANDBOX_WORKSPACE", "/var/lib/wiserecipes/sandboxes")
_runner: SandboxRunner | None = None


def get_runner() -> SandboxRunner:
    """Return the singleton SandboxRunner instance, creating it if needed."""
    global _runner
    if _runner is None:
        _runner = SandboxRunner(workspace=SANDBOX_WORKSPACE)
    return _runner


# ── Schemas ──────────────────────────────────────────────────────────────


class SandboxRunRequest(BaseModel):
    """Request body for POST /api/skills/{slug}/sandbox/run."""

    entrypoint: str = Field("setup.sh", description="Script to execute inside sandbox")
    version: str | None = Field(None, description="Specific version (default: latest)")
    env: dict[str, str] | None = Field(None, description="Extra env vars for sandbox")


class SandboxRunResponse(BaseModel):
    """Response for sandbox execution."""

    sandbox_id: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    success: bool
    error: str | None = None


class SandboxStatusResponse(BaseModel):
    """Response for sandbox status check."""

    slug: str
    sandbox_supported: bool
    profile: dict | None = None
    validation_warnings: list[str] = Field(default_factory=list)


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/skills/{slug}/sandbox/status", response_model=SandboxStatusResponse)
def sandbox_status(slug: str, db: Session = Depends(get_db)):
    """Check if a skill supports sandbox execution and return its profile."""
    skill = db.query(Skill).options(joinedload(Skill.versions)).filter(Skill.slug == slug).first()
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")

    # Check latest version for skill_toml with [sandbox] block
    latest = skill.versions[0] if skill.versions else None
    if not latest or not latest.skill_toml:
        return SandboxStatusResponse(
            slug=slug,
            sandbox_supported=False,
            validation_warnings=["No skill.toml manifest found in latest version"],
        )

    try:
        profile = SandboxProfile.from_manifest(latest.skill_toml)
    except ValueError as exc:
        return SandboxStatusResponse(
            slug=slug,
            sandbox_supported=False,
            validation_warnings=[str(exc)],
        )

    warnings = profile.validate()
    has_sandbox_block = "sandbox" in _parse_toml_keys(latest.skill_toml)

    return SandboxStatusResponse(
        slug=slug,
        sandbox_supported=has_sandbox_block,
        profile={
            "network_allow": profile.network_allow,
            "fs_write": profile.fs_write,
            "exec_allow": profile.exec_allow,
            "memory_mb": profile.memory_mb,
            "timeout_seconds": profile.timeout_seconds,
            "env_pass": profile.env_pass,
        },
        validation_warnings=warnings,
    )


@router.post("/skills/{slug}/sandbox/run", response_model=SandboxRunResponse)
def sandbox_run(
    slug: str,
    request: Request,
    body: SandboxRunRequest,  # Issue #26 fix: parse body from request JSON
    db: Session = Depends(get_db),
):
    """Execute a skill's entrypoint inside a bubblewrap sandbox.

    The skill must have a [sandbox] block in its skill.toml manifest.
    Execution result is recorded as a telemetry event.
    Requires master scope or is_sandbox_operator=True on the API key.
    """
    # Issue authz gate: only master or is_sandbox_operator may run sandboxes
    auth_ctx = getattr(request.state, "auth_ctx", None)
    if auth_ctx is None or not authz.can_run_sandbox(auth_ctx):
        return JSONResponse(
            status_code=403,
            content={
                "detail": "Forbidden: sandbox execution requires master scope or is_sandbox_operator=True"
            },
        )
    skill = db.query(Skill).options(joinedload(Skill.versions)).filter(Skill.slug == slug).first()
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")

    # Find the right version
    if body.version:
        version = next((v for v in skill.versions if v.semver == body.version), None)
        if not version:
            raise HTTPException(status_code=404, detail=f"Version '{body.version}' not found")
    else:
        version = skill.versions[0] if skill.versions else None

    if not version:
        raise HTTPException(status_code=404, detail="No versions available")

    # Parse sandbox profile from manifest
    if not version.skill_toml:
        raise HTTPException(
            status_code=400,
            detail=f"Skill '{slug}' has no skill.toml manifest — sandbox requires [sandbox] block",
        )

    try:
        profile = SandboxProfile.from_manifest(version.skill_toml)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Check for [sandbox] block presence
    if "sandbox" not in _parse_toml_keys(version.skill_toml):
        raise HTTPException(
            status_code=400,
            detail=f"Skill '{slug}' skill.toml has no [sandbox] block — not eligible for sandbox execution",
        )

    # Validate profile
    warnings = profile.validate()
    if any("dangerous" in w for w in warnings):
        raise HTTPException(status_code=400, detail=f"Sandbox profile validation failed: {warnings}")

    # Resolve skill directory (from tarball_path or checkout)
    skill_dir = _resolve_skill_dir(skill.slug, version)
    if not skill_dir or not os.path.isdir(skill_dir):
        raise HTTPException(
            status_code=500,
            detail=f"Skill checkout directory not found for '{slug}'. Tarball path: {version.tarball_path}",
        )

    # Run in sandbox
    runner = get_runner()
    result = runner.run(
        skill_dir=skill_dir,
        entrypoint=body.entrypoint,
        profile=profile,
        skill_slug=slug,
        env=body.env,
    )

    # Record telemetry
    event = TelemetryEvent(
        event_type="sandbox_run",
        skill_slug=slug,
        payload=json.dumps(
            {
                "sandbox_id": result.sandbox_id,
                "entrypoint": body.entrypoint,
                "version": version.semver,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "duration_seconds": round(result.duration_seconds, 3),
                "success": result.success,
                "error": result.error,
            }
        ),
    )
    db.add(event)
    db.commit()

    return SandboxRunResponse(
        sandbox_id=result.sandbox_id,
        exit_code=result.exit_code,
        stdout=result.stdout[:5000],
        stderr=result.stderr[:5000],
        timed_out=result.timed_out,
        duration_seconds=round(result.duration_seconds, 3),
        success=result.success,
        error=result.error,
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _parse_toml_keys(toml_str: str) -> set[str]:
    """Extract top-level table names from a TOML string (lightweight parser)."""
    import re

    return {m.group(1) for m in re.finditer(r"^\[(\w+)\]", toml_str, re.MULTILINE)}


def _resolve_skill_dir(slug: str, version: SkillVersion) -> str | None:
    """Resolve the host directory for a skill checkout.

    Checks:
      1. tarball_path as directory (extracted checkout)
      2. /var/lib/wiserecipes/skills/{slug}/ (standard checkout path)
    """
    if version.tarball_path:
        # If tarball_path is a directory, use it directly
        if os.path.isdir(version.tarball_path):
            return version.tarball_path
        # If it's a .tar.gz, look for extracted version nearby
        if version.tarball_path.endswith((".tar.gz", ".tgz")):
            extracted = version.tarball_path.rsplit(".", 2)[0]
            if os.path.isdir(extracted):
                return extracted

    # Standard checkout path
    standard = f"/var/lib/wiserecipes/skills/{slug}"
    if os.path.isdir(standard):
        return standard

    # Development path (local repo)
    dev = f"/home/wisechef/wiserecipes-api/dev-skills/{slug}"
    if os.path.isdir(dev):
        return dev

    return None
