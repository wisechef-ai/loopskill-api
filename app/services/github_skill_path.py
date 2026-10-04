"""Resolve a skill's SKILL.md inside a known public GitHub repo (fed1005).

skills.sh cards and hub rows mirrored from skills.sh name a repo and a skill
id, not the in-repo path. The old resolver spent two ANONYMOUS api.github.com
calls per install (60/h for the whole prod IP), so after the first ~30 installs
in an hour every card returned 404 ``unresolvable``.

Resolution order (each step only when the previous one missed):

1. Raw CDN at ref ``HEAD`` on the conventional paths. ``HEAD`` follows the
   default branch, so no default-branch lookup is needed. Measured on 52
   resolvable prod failures: these paths cover 44 (85%) with ZERO API calls.
2. A root ``SKILL.md`` whose frontmatter ``name`` equals the id.
3. ONE tree walk (``git/trees/HEAD?recursive=1``), authed when GITHUB_TOKEN /
   GH_TOKEN exists (5,000/h instead of 60/h). Match by parent-dir basename; a
   root SKILL.md counts only when it is the repo's ONLY skill. A multi-skill
   repo with no match returns None — never another skill's body.

Hits cache the raw URL; misses are negatively cached so a dead repo cannot
drain the quota. The token goes to api.github.com only, never to the CDN.
"""

from __future__ import annotations

import json
import logging
import os
import re

from app.services._ttl_cache import TTLCache
from app.services.federation_fetch import guarded_get

logger = logging.getLogger(__name__)

RAW_BASE = "https://raw.githubusercontent.com"
TREES_URL = "https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1"
# Ordered by measured frequency on prod (fed1005 sample of 52 resolvable).
CONVENTIONAL_PATHS = (
    "skills/{id}/SKILL.md",
    "{id}/SKILL.md",
    ".claude/skills/{id}/SKILL.md",
    ".agents/skills/{id}/SKILL.md",
)
HIT_TTL_S = 6 * 3600.0
MISS_TTL_S = 900.0
_MISS = "-"
_TIMEOUT_S = 12.0

_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_FM_NAME = re.compile(r"^name:\s*['\"]?([^'\"\n#]+?)['\"]?\s*$", re.M)

_cache = TTLCache()


def _safe(repo: str, skill_id: str) -> bool:
    parts = (repo or "").split("/")
    if len(parts) != 2 or not skill_id:
        return False
    return all(_SEGMENT.match(p) and p not in (".", "..") for p in (*parts, skill_id))


def github_api_headers() -> dict[str, str]:
    """Headers for an api.github.com READ — authed when a token exists (5,000/h
    instead of the 60/h anonymous quota the whole prod IP shares). Send these to
    api.github.com only; ``guarded_get`` strips them on a cross-host redirect."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _raw(repo: str, path: str) -> str | None:
    resp = guarded_get(f"{RAW_BASE}/{repo}/HEAD/{path}", timeout=_TIMEOUT_S)
    if resp is not None and resp.status_code == 200 and (resp.text or "").strip():
        return resp.text
    return None


def frontmatter_name(body: str) -> str | None:
    if not body.startswith("---"):
        return None
    end = body.find("\n---", 3)
    m = _FM_NAME.search(body[: end if end > 0 else 2000])
    return m.group(1).strip() if m else None


def _tree_paths(repo: str) -> list[str] | None:
    headers = github_api_headers()
    resp = guarded_get(TREES_URL.format(repo=repo), timeout=_TIMEOUT_S, headers=headers)
    if resp is None or resp.status_code != 200:
        if resp is not None and resp.status_code in (403, 429):
            logger.warning(
                "github tree walk rate-limited for %s (authed=%s)", repo, "Authorization" in headers
            )
        return None
    try:
        tree = json.loads(resp.text).get("tree", [])
    except (ValueError, AttributeError):
        return None
    return [
        str(t.get("path", ""))
        for t in tree
        if isinstance(t, dict) and str(t.get("path", "")).endswith("SKILL.md")
    ]


def _locate(repo: str, skill_id: str) -> tuple[str, str] | None:
    """Return (in-repo path, body) or None."""
    for shape in CONVENTIONAL_PATHS:
        path = shape.format(id=skill_id)
        body = _raw(repo, path)
        if body:
            return path, body
    root = _raw(repo, "SKILL.md")
    if root and frontmatter_name(root) == skill_id:
        return "SKILL.md", root
    paths = _tree_paths(repo)
    if not paths:
        return None
    match = next((p for p in paths if p.rsplit("/", 2)[-2:-1] == [skill_id]), None)
    if match is None and paths == ["SKILL.md"]:
        match = "SKILL.md"
    if match is None:
        return None
    if match == "SKILL.md" and root:
        return match, root
    body = _raw(repo, match)
    return (match, body) if body else None


def resolve_repo_skill_md(repo: str, skill_id: str) -> tuple[str, str] | None:
    """(raw_url, SKILL.md body) for ``skill_id`` in public repo ``repo``, or None."""
    if not _safe(repo, skill_id):
        return None
    key = f"gh-skill-path:{repo}:{skill_id}"
    cached = _cache.get(key, HIT_TTL_S)
    if cached == _MISS:
        if _cache.get(key, MISS_TTL_S) == _MISS:
            return None
    elif cached:
        body = _raw(repo, cached)
        if body:
            return f"{RAW_BASE}/{repo}/HEAD/{cached}", body
    found = _locate(repo, skill_id)
    if found is None:
        _cache.put(key, _MISS)
        return None
    path, body = found
    _cache.put(key, path)
    return f"{RAW_BASE}/{repo}/HEAD/{path}", body
