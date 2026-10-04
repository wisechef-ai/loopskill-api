"""Resolve a skill's SKILL.md inside a known public GitHub repo (fed1005).

skills.sh cards and hub rows mirrored from skills.sh name a repo and a skill
id, not the in-repo path. The old resolver spent two ANONYMOUS api.github.com
calls per install (60/h for the whole prod IP), so after the first ~30 installs
in an hour every card returned 404 ``unresolvable``.

Resolution (each wave only when the previous one found nothing):

1. ONE parallel wave of raw-CDN GETs at ref ``HEAD`` (follows the default
   branch): an optional hint path, the conventional paths, and the root
   ``SKILL.md``. The first candidate in priority order that passes the
   identity rule wins. Measured on 52 resolvable prod failures: this wave
   alone covers 44 (85%) with ZERO API calls.
2. ONE tree walk (``git/trees/HEAD?recursive=1``), authed when GITHUB_TOKEN /
   GH_TOKEN exists (5,000/h instead of 60/h). Only blobs named exactly
   ``SKILL.md`` count. The root counts only when it is the repo's ONLY skill
   (a single-skill repo may publish under an alias id).
3. Raw GETs for the tree's directory matches (at most ``_MAX_TREE_MATCHES``,
   in parallel). Exactly one may pass the identity rule, else None.

Worst case: 3 sequential round trips — ≤ 6 parallel raw GETs, 1 API GET,
≤ 3 parallel raw GETs.

Identity rule (C1 — never install another skill's body): a body is accepted
for ``skill_id`` when its frontmatter ``name`` (slugified) equals the id, or it
has no parseable name AND its directory is named ``skill_id``. A name that
contradicts the id is rejected even at a matching path. Measured: 42/42
directory hits on prod carry name == id, so this rejects nothing legitimate.

Hits cache the path for 6 h and are re-checked against the identity rule on
every use; a moved path is resolved again. Misses are cached for 15 min. The
first resolution of a key is single-flight, so a burst of installs for one dead
repo costs one walk. The token goes to api.github.com only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor

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
_TIMEOUT_S = 12.0
_MAX_WAVE = 6
_MAX_TREE_MATCHES = 3

_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_FM_NAME = re.compile(r"^name:[ \t]*(.*?)[ \t]*$", re.M)

_cache = TTLCache()
_flight_guard = threading.Lock()
_flights: dict[str, threading.Lock] = {}


def _segment_ok(part: str) -> bool:
    return bool(_SEGMENT.match(part)) and part not in (".", "..")


def _safe(repo: str, skill_id: str) -> bool:
    parts = (repo or "").split("/")
    if len(parts) != 2 or not skill_id:
        return False
    return all(_segment_ok(p) for p in (*parts, skill_id))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def github_api_headers() -> dict[str, str]:
    """Headers for an api.github.com READ — authed when a token exists (5,000/h
    instead of the 60/h anonymous quota the whole prod IP shares). Send these to
    api.github.com only; ``guarded_get`` strips them when a redirect changes
    scheme or host."""
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def frontmatter_name(body: str) -> str | None:
    """The ``name`` of a COMPLETE YAML frontmatter block (opening AND closing
    ``---`` fence), or None. BOM and CRLF tolerated; block scalars ignored."""
    text = (body or "").lstrip("\ufeff").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    m = _FM_NAME.search(text[4 : end + 1])
    if not m:
        return None
    value = m.group(1).split(" #", 1)[0].strip().strip("'\"").strip()
    if not value or value[0] in "|>":
        return None
    return value


def _dir_of(path: str) -> str:
    return path.rsplit("/", 2)[-2] if "/" in path else ""


def _identity_ok(path: str, body: str, skill_id: str) -> bool:
    name = frontmatter_name(body)
    if name is None:
        return _dir_of(path) == skill_id
    return _slug(name) == _slug(skill_id)


def _raw(repo: str, path: str) -> str | None:
    resp = guarded_get(f"{RAW_BASE}/{repo}/HEAD/{path}", timeout=_TIMEOUT_S)
    if resp is not None and resp.status_code == 200 and (resp.text or "").strip():
        return resp.text
    return None


def _raw_many(repo: str, paths: list[str]) -> list[str | None]:
    if len(paths) == 1:
        return [_raw(repo, paths[0])]
    with ThreadPoolExecutor(max_workers=min(_MAX_WAVE, len(paths))) as pool:
        return list(pool.map(lambda p: _raw(repo, p), paths))


def _tree_paths(repo: str) -> list[str] | None:
    """Paths of every blob named exactly SKILL.md, or None on any failure."""
    headers = github_api_headers()
    resp = guarded_get(TREES_URL.format(repo=repo), timeout=_TIMEOUT_S, headers=headers)
    if resp is None or resp.status_code != 200:
        if resp is not None and resp.status_code in (403, 429):
            logger.warning(
                "github tree walk rate-limited for %s (authed=%s)", repo, "Authorization" in headers
            )
        return None
    try:
        data = json.loads(resp.text)
    except (TypeError, ValueError):
        return None
    tree = data.get("tree") if isinstance(data, dict) else None
    if not isinstance(tree, list):
        return None
    out: list[str] = []
    for entry in tree:
        if not isinstance(entry, dict) or entry.get("type", "blob") != "blob":
            continue
        path = entry.get("path")
        if isinstance(path, str) and (path == "SKILL.md" or path.endswith("/SKILL.md")):
            out.append(path)
    return out


def _locate(repo: str, skill_id: str, hint_path: str | None) -> tuple[str, str] | None:
    """Return (in-repo path, body) or None."""
    candidates: list[str] = []
    if hint_path:
        candidates.append(f"{hint_path.strip('/')}/SKILL.md")
    candidates += [shape.format(id=skill_id) for shape in CONVENTIONAL_PATHS]
    candidates.append("SKILL.md")
    candidates = list(dict.fromkeys(candidates))
    bodies = dict(zip(candidates, _raw_many(repo, candidates)))
    for path in candidates:
        body = bodies[path]
        if body and _identity_ok(path, body, skill_id):
            return path, body

    tree = _tree_paths(repo)
    if not tree:
        return None
    root = bodies.get("SKILL.md")
    if tree == ["SKILL.md"] and root:
        return "SKILL.md", root  # single-skill repo: alias id allowed
    matches = [p for p in tree if p not in bodies and _dir_of(p) == skill_id][:_MAX_TREE_MATCHES]
    if not matches:
        return None
    passing = [
        (p, b) for p, b in zip(matches, _raw_many(repo, matches)) if b and _identity_ok(p, b, skill_id)
    ]
    return passing[0] if len(passing) == 1 else None


def _flight_lock(key: str) -> threading.Lock:
    with _flight_guard:
        return _flights.setdefault(key, threading.Lock())


def resolve_repo_skill_md(
    repo: str, skill_id: str, *, hint_path: str | None = None
) -> tuple[str, str] | None:
    """(raw_url, SKILL.md body) for ``skill_id`` in public repo ``repo``, or None."""
    if not _safe(repo, skill_id):
        return None
    if hint_path is not None and not all(_segment_ok(p) for p in hint_path.strip("/").split("/")):
        hint_path = None
    hit_key = f"gh-skill-path:{repo}:{skill_id}"
    miss_key = f"{hit_key}:miss"
    with _flight_lock(hit_key):
        if _cache.get(miss_key, MISS_TTL_S):
            return None
        cached = _cache.get(hit_key, HIT_TTL_S)
        if cached:
            body = _raw(repo, cached)
            if body and (cached == "SKILL.md" or _identity_ok(cached, body, skill_id)):
                return f"{RAW_BASE}/{repo}/HEAD/{cached}", body
        found = _locate(repo, skill_id, hint_path)
        if found is None:
            _cache.put(miss_key, True)
            return None
        path, body = found
        _cache.put(hit_key, path)
        _cache.put(miss_key, None)
        return f"{RAW_BASE}/{repo}/HEAD/{path}", body


def resolve_skills_sh_slug(slug: str) -> tuple[str, str] | None:
    """Decode a skills.sh slug (``owner--repo--skillId``: '/' joined with '--')
    and resolve it. GitHub owners cannot contain '--', but repo names and skill
    ids can (17 of 20,000 prod ids contain '---'), so the split between repo and
    skill is ambiguous. Every split is tried; the slug resolves only when
    EXACTLY ONE split resolves (C1: never guess between two repos)."""
    owner, sep, rest = (slug or "").partition("--")
    if not sep or not owner or not rest:
        return None
    splits: list[tuple[str, str]] = []
    start = 0
    while (idx := rest.find("--", start)) >= 0:
        repo, skill = rest[:idx], rest[idx + 2 :]
        if repo and skill:
            splits.append((f"{owner}/{repo}", skill))
        start = idx + 1
    if len(splits) == 1:
        return resolve_repo_skill_md(*splits[0])
    hits = [r for r in (resolve_repo_skill_md(repo, sid) for repo, sid in splits) if r]
    return hits[0] if len(hits) == 1 else None
