"""Skill detail surface API — file manifest, single-file, install events, runtime.

Phase Q (topshelf_2605) — four new read-only endpoints that expose per-skill
contents and metadata that were previously invisible.

Routes registered:
  GET /skills/{slug}/files           — tarball file manifest
  GET /skills/{slug}/file            — single file content from tarball
  GET /skills/{slug}/install-events  — install sparkline bucketed by day
  GET /skills/{slug}/runtime         — runtime/toolchain detection from frontmatter
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import re
import time
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session  # type: ignore[import]
from sqlalchemy.orm import joinedload  # type: ignore[import]

from app.database import get_db
from app.models import InstallEvent, Skill

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])

# ── Constants ──────────────────────────────────────────────────────────────

_MAX_FILE_BYTES = 1 * 1024 * 1024  # 1 MiB
_MAX_PATH_LEN = 256

# Install-events in-memory cache: slug → (timestamp, payload)
_INSTALL_EVENTS_CACHE: dict[str, tuple[float, dict]] = {}
_INSTALL_EVENTS_TTL = 3600  # 1 hour

# Category → runtime inference map
_CATEGORY_RUNTIME_MAP: dict[str, list[str]] = {
    "python": ["python"],
    "javascript": ["node"],
    "js": ["node"],
    "typescript": ["node"],
    "ts": ["node"],
    "shell": ["bash"],
    "bash": ["bash"],
    "docker": ["docker"],
    "go": ["go"],
    "rust": ["rust"],
    "ruby": ["ruby"],
    "java": ["java"],
    "devops": ["bash", "docker"],
    "data": ["python"],
    "automation": ["python"],
    "ml": ["python"],
    "ai": ["python"],
}


# ── Path-validation helper ─────────────────────────────────────────────────


def _validate_file_path(path: str) -> str:
    """Return the validated path or raise HTTPException(400).

    Security checks (CVE-class):
      - Null bytes (%00 / \\x00)
      - Absolute paths (starts with /)
      - .. components (after URL-decode — FastAPI decodes query params before us)
      - Encoded traversal via %2e%2e (raw; FastAPI auto-decodes, but we check
        the decoded string which catches %2e%2e too after decoding → ..
        The path arrives already decoded, so checking for ".." is sufficient.)
      - Encoded slash %2f (not needed; FastAPI normalises before our handler)
      - Path length > 256 chars
      - Symlinks (rejected at extraction time in skill_file_cache)
    """
    if not path:
        raise HTTPException(status_code=400, detail="path must not be empty")

    # Null bytes
    if "\x00" in path or "%00" in path:
        raise HTTPException(status_code=400, detail="Invalid path: null byte")

    # Absolute path
    if path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path: absolute paths not allowed")

    # Length cap
    if len(path) > _MAX_PATH_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid path: exceeds {_MAX_PATH_LEN} characters",
        )

    # Traversal via .. in any component (also catches %2e%2e after URL decode)
    import pathlib

    try:
        pp = pathlib.PurePosixPath(path)
    # Rationale: any parse failure on attacker-controlled path input must fail
    # closed as a 400, never propagate — defensive for the path-traversal guard.
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid path")

    for part in pp.parts:
        if part in ("..", ".") and part != ".":
            # Allow leading '.' only in filenames (e.g. .gitignore), not as ..
            pass
        if part == "..":
            raise HTTPException(status_code=400, detail="Invalid path: traversal not allowed")

    # Also catch raw %2e%2e that somehow survived (belt-and-suspenders)
    if "%2e%2e" in path.lower() or "%2f" in path.lower() and ".." in path:
        raise HTTPException(status_code=400, detail="Invalid path: traversal not allowed")

    return path


# ── Skill lookup helper ────────────────────────────────────────────────────


def _get_public_skill(slug: str, db: Session) -> Skill:
    skill = (
        db.query(Skill)
        .options(joinedload(Skill.versions))
        .filter(
            Skill.slug == slug,
            Skill.is_public == True,  # noqa: E712
            Skill.is_archived == False,  # noqa: E712
        )
        .first()
    )
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{slug}' not found")
    return skill


def _get_latest_version_and_tarball(skill: Skill):
    """Return (version, tarball_path) or raise 404."""
    if not skill.versions:
        raise HTTPException(status_code=404, detail=f"No versions available for '{skill.slug}'")
    latest = skill.versions[0]
    if not latest.tarball_path:
        raise HTTPException(
            status_code=404,
            detail=f"No tarball path recorded for '{skill.slug}@{latest.semver}'",
        )
    import pathlib

    tar_path = pathlib.Path(latest.tarball_path)
    if not tar_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"Tarball file missing for '{skill.slug}@{latest.semver}'",
        )
    return latest, str(tar_path)


# ── Q.1.1 — File manifest ─────────────────────────────────────────────────


@router.get("/skills/{slug}/files", tags=["skills"])
def get_skill_files(slug: str, db: Session = Depends(get_db)):
    """Return the tarball file manifest for a skill.

    Response: {version, files: [{path, size, type}], total_files, total_bytes}

    Cached at version level (immutable per checksum_sha256).
    No auth required — only the listing, not the content.
    """
    from app import skill_file_cache

    skill = _get_public_skill(slug, db)
    latest, tarball_path = _get_latest_version_and_tarball(skill)

    cache_data = skill_file_cache.get_or_build(
        skill_id=str(skill.id),
        version_id=str(latest.id),
        checksum_sha256=latest.checksum_sha256 or "",
        tarball_path=tarball_path,
    )

    manifest = cache_data["manifest"]
    total_bytes = sum(f["size"] for f in manifest if f["type"] == "file")

    return {
        "version": latest.semver,
        "files": manifest,
        "total_files": sum(1 for f in manifest if f["type"] == "file"),
        "total_bytes": total_bytes,
    }


# ── Q.1.2 — Single file content ───────────────────────────────────────────


@router.get("/skills/{slug}/file", tags=["skills"])
def get_skill_file(
    slug: str,
    request: Request,
    path: str = Query(..., description="Relative file path within the skill tarball"),
    db: Session = Depends(get_db),
):
    """Return the content of a single file from the skill tarball.

    SECURITY CRITICAL:
      - Path traversal, absolute paths, null bytes, symlinks → 400
      - Path length > 256 chars → 400
      - File > 1 MiB → 413
      - Free tier: SKILL.md only; pro/master: all files → 403 if gated
      - Auth via authz.can_read_skill; relies on RateLimitMiddleware for rate limits
    """
    from app import skill_file_cache
    from app.authz import can_read_skill
    from app.tier_labels import _is_paid_tier

    # 1. Path validation — always first, before any DB/IO
    _validate_file_path(path)

    # 2. Skill lookup
    skill = _get_public_skill(slug, db)

    # 3. Auth context
    auth_ctx = getattr(request.state, "auth_ctx", None) if request else None
    caller_tier = auth_ctx.tier if auth_ctx is not None else None
    caller_is_master = getattr(auth_ctx, "scope", None) == "master"
    caller_is_paid = caller_is_master or _is_paid_tier(caller_tier)

    # 4. can_read_skill gate
    if auth_ctx is not None and not can_read_skill(auth_ctx, skill, db=db):
        raise HTTPException(status_code=403, detail="Access denied")

    # 5. Tier paywall: free callers may only see SKILL.md
    #    Skill-level free-tier exemption: if the skill itself is free-tier, allow all
    skill_is_free = (skill.tier or "").lower() == "free"
    if not caller_is_paid and not skill_is_free:
        if path != "SKILL.md":
            raise HTTPException(
                status_code=403,
                detail="Pro subscription required to access non-SKILL.md files",
            )

    # 6. Tarball I/O via cache
    latest, tarball_path = _get_latest_version_and_tarball(skill)

    cache_data = skill_file_cache.get_or_build(
        skill_id=str(skill.id),
        version_id=str(latest.id),
        checksum_sha256=latest.checksum_sha256 or "",
        tarball_path=tarball_path,
    )

    # 7. Symlink check: reject symlinks before content delivery
    manifest = cache_data["manifest"]
    for entry in manifest:
        if entry["path"] == path and entry["type"] == "symlink":
            raise HTTPException(status_code=400, detail="Invalid path: symlinks not allowed")

    # 8. File existence
    files: dict[str, bytes] = cache_data["files"]
    if path not in files:
        raise HTTPException(
            status_code=404,
            detail=f"File '{path}' not found in skill tarball",
        )

    content_bytes = files[path]

    # 9. Size cap
    if len(content_bytes) > _MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File '{path}' exceeds 1 MiB size limit",
        )

    # 10. Content-type sniff and response
    mime_type, _ = mimetypes.guess_type(path)
    # Treat None as text/plain for known text-like extensions; binary otherwise
    text_mimes = {
        "text/",
        "application/json",
        "application/xml",
        "application/javascript",
        "application/x-sh",
        "application/toml",
    }
    is_text = mime_type is None or any(mime_type.startswith(tm) for tm in text_mimes)

    # Shell scripts and .md/.toml commonly lack MIME recognition — treat as text
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    text_exts = {
        "md",
        "txt",
        "toml",
        "yaml",
        "yml",
        "json",
        "sh",
        "py",
        "js",
        "ts",
        "rb",
        "go",
        "rs",
        "java",
        "css",
        "html",
        "xml",
        "env",
        "cfg",
        "ini",
        "conf",
        "lock",
        "log",
    }
    if ext in text_exts:
        is_text = True

    text_content: str | None = None
    if is_text:
        try:
            text_content = content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            # Fallback: serve as binary base64
            is_text = False
            text_content = None

    if is_text and text_content is not None:
        return {
            "path": path,
            "encoding": "utf-8",
            "content": text_content,
            "size": len(content_bytes),
            "content_type": mime_type or "text/plain",
        }
    else:
        return {
            "path": path,
            "encoding": "base64",
            "content": base64.b64encode(content_bytes).decode("ascii"),
            "size": len(content_bytes),
            "content_type": mime_type or "application/octet-stream",
        }


# ── Q.1.3 — Install sparkline ─────────────────────────────────────────────


@router.get("/skills/{slug}/install-events", tags=["skills"])
def get_skill_install_events(
    slug: str,
    window: str = Query("7d", description="Time window: '7d' or '30d'"),
    db: Session = Depends(get_db),
):
    """Return install event counts bucketed by day for a sparkline chart.

    Response: {window_days, buckets: [{date, count}], total_in_window, total_all_time}

    Public — no auth required.  Cached 1h in-process.
    """
    if window not in ("7d", "30d"):
        raise HTTPException(status_code=400, detail="window must be '7d' or '30d'")

    skill = _get_public_skill(slug, db)
    cache_key = f"{skill.id}:{window}"
    now_ts = time.monotonic()

    # Check in-memory cache
    cached = _INSTALL_EVENTS_CACHE.get(cache_key)
    if cached is not None:
        cached_at, payload = cached
        if now_ts - cached_at < _INSTALL_EVENTS_TTL:
            return payload

    window_days = int(window.rstrip("d"))
    since = datetime.now(UTC) - timedelta(days=window_days)

    # All-time count
    total_all_time = db.query(InstallEvent).filter(InstallEvent.skill_id == skill.id).count()

    # Events within window
    window_events = (
        db.query(InstallEvent)
        .filter(
            InstallEvent.skill_id == skill.id,
            InstallEvent.created_at >= since,
        )
        .all()
    )

    # Build day-bucketed counts
    bucket_counts: dict[date, int] = {}
    for event in window_events:
        ev_date = event.created_at
        if ev_date is not None:
            # created_at may be naive (SQLite) or aware (Postgres)
            if hasattr(ev_date, "date"):
                day = ev_date.date() if hasattr(ev_date, "date") else date.today()
            else:
                day = date.today()
            bucket_counts[day] = bucket_counts.get(day, 0) + 1

    # Fill all days in window with 0 for days with no events
    buckets = []
    for i in range(window_days - 1, -1, -1):
        d = (datetime.now(UTC) - timedelta(days=i)).date()
        buckets.append({"date": d.isoformat(), "count": bucket_counts.get(d, 0)})

    payload = {
        "window_days": window_days,
        "buckets": buckets,
        "total_in_window": len(window_events),
        "total_all_time": total_all_time,
    }

    _INSTALL_EVENTS_CACHE[cache_key] = (now_ts, payload)
    return payload


# ── Q.1.4 — Runtime detection ─────────────────────────────────────────────

# YAML frontmatter runtime keys to look for
_RUNTIME_KEYS = ("runtime", "runtimes", "compatible", "requires", "tools")


def _parse_frontmatter_runtimes(readme: str | None) -> dict:
    """Extract runtime/tools info from SKILL.md YAML frontmatter.

    Returns {runtimes: list[str], tools_required: list[str], frontmatter_present: bool}.
    """
    if not readme:
        return {"runtimes": [], "tools_required": [], "frontmatter_present": False}

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", readme, re.DOTALL)
    if not match:
        return {"runtimes": [], "tools_required": [], "frontmatter_present": False}

    yaml_block = match.group(1)
    runtimes: list[str] = []
    tools: list[str] = []

    try:
        import yaml

        data = yaml.safe_load(yaml_block) or {}
        if not isinstance(data, dict):
            return {"runtimes": [], "tools_required": [], "frontmatter_present": True}

        for key in ("runtime", "runtimes", "compatible"):
            val = data.get(key)
            if val:
                if isinstance(val, list):
                    runtimes.extend(str(v) for v in val)
                elif isinstance(val, str):
                    runtimes.append(val)

        for key in ("tools", "requires"):
            val = data.get(key)
            if val:
                if isinstance(val, list):
                    tools.extend(str(v) for v in val)
                elif isinstance(val, str):
                    tools.append(val)

    except Exception:  # noqa: BLE001
        # Rationale: malformed YAML frontmatter must not break the runtime endpoint
        pass

    return {
        "runtimes": list(dict.fromkeys(runtimes)),  # dedupe, preserve order
        "tools_required": list(dict.fromkeys(tools)),
        "frontmatter_present": True,
    }


def _infer_runtimes_from_category(category: str | None) -> list[str]:
    """Infer likely runtimes from the skill's category field."""
    if not category:
        return []
    return _CATEGORY_RUNTIME_MAP.get(category.lower(), [])


@router.get("/skills/{slug}/runtime", tags=["skills"])
def get_skill_runtime(slug: str, db: Session = Depends(get_db)):
    """Return runtime/toolchain detection info for a skill.

    Parses SKILL.md frontmatter for runtime/compatible/tools/requires keys.
    Falls back to category-based inference when frontmatter has no runtime keys.

    Response: {runtimes, tools_required, frontmatter_present, inferred}

    Public — no auth required.
    """
    skill = _get_public_skill(slug, db)

    fm_data = _parse_frontmatter_runtimes(skill.readme)
    frontmatter_present = fm_data["frontmatter_present"]
    runtimes = fm_data["runtimes"]
    tools_required = fm_data["tools_required"]
    inferred = False

    # Fall back to category inference when frontmatter has no runtime info
    if not runtimes:
        inferred_runtimes = _infer_runtimes_from_category(skill.category)
        if inferred_runtimes:
            runtimes = inferred_runtimes
            inferred = True

    return {
        "runtimes": runtimes,
        "tools_required": tools_required,
        "frontmatter_present": frontmatter_present,
        "inferred": inferred,
    }
