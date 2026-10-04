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

Worst case: 4 sequential round trips, 11 GETs — a stale cached path (1),
≤ 6 parallel raw GETs, 1 API GET, ≤ 3 parallel raw GETs. Raw waves share
one 16-worker pool; single-flight uses 64 striped locks (bounded memory).

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

_cache = TTLCache()
# Bounded resources: one shared pool for every raw wave, and a fixed set of
# striped locks for single-flight (same key -> same lock; memory never grows).
_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="gh-skill-path")
_STRIPES = tuple(threading.Lock() for _ in range(64))


def _segment_ok(part: str) -> bool:
    return bool(_SEGMENT.match(part)) and part not in (".", "..")


def _safe(repo: str, skill_id: str) -> bool:
    parts = (repo or "").split("/")
    if len(parts) != 2 or not skill_id:
        return False
    return all(_segment_ok(p) for p in (*parts, skill_id))


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


_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
MAX_FRONTMATTER_BYTES = 8192
_INVALID = object()  # an explicit name that cannot be trusted -> fail closed


def _frontmatter_name_state(body: str) -> object:
    """None (no frontmatter / no ``name`` key), a name string, or _INVALID
    (present but non-string/empty, YAML that does not parse, or a block over
    MAX_FRONTMATTER_BYTES). Untrusted YAML is bounded BEFORE parsing and ANY
    parser failure (RecursionError included) counts as invalid (fed1005 R3)."""
    import yaml

    text = (body or "").lstrip("\ufeff").replace("\r\n", "\n")
    m = _FRONTMATTER.match(text)
    if not m:
        return None
    block = m.group(1)
    if len(block.encode("utf-8", "replace")) > MAX_FRONTMATTER_BYTES:
        return _INVALID
    try:
        data = yaml.safe_load(block)
    except Exception:  # noqa: BLE001 — YAMLError, RecursionError, MemoryError ...
        return _INVALID
    if not isinstance(data, dict) or "name" not in data:
        return None
    name = data["name"]
    return name.strip() if isinstance(name, str) and name.strip() else _INVALID


def frontmatter_name(body: str) -> str | None:
    """The trusted frontmatter ``name`` string, or None."""
    state = _frontmatter_name_state(body)
    return state if isinstance(state, str) else None


def _dir_of(path: str) -> str:
    return path.rsplit("/", 2)[-2] if "/" in path else ""


def _name_key(value: str) -> str:
    """Case and whitespace are folded ('Convex Best Practices' ==
    'convex-best-practices'); '.', '_' and '-' stay distinct, because a repo may
    hold separate 'foo.bar' and 'foo-bar' skills (fed1005 R3)."""
    return re.sub(r"\s+", "-", value.strip()).casefold()


def _identity_ok(path: str, body: str, skill_id: str) -> bool:
    state = _frontmatter_name_state(body)
    if state is _INVALID:
        return False
    if state is None:
        return _dir_of(path) == skill_id
    return _name_key(state) == _name_key(skill_id)


def _raw(repo: str, path: str) -> str | None:
    resp = guarded_get(f"{RAW_BASE}/{repo}/HEAD/{path}", timeout=_TIMEOUT_S)
    if resp is not None and resp.status_code == 200 and (resp.text or "").strip():
        return resp.text
    return None


def _raw_many(repo: str, paths: list[str]) -> list[str | None]:
    if len(paths) == 1:
        return [_raw(repo, paths[0])]
    return list(_POOL.map(lambda p: _raw(repo, p), paths[:_MAX_WAVE]))


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


def _locate(repo: str, skill_id: str, hint_path: str | None) -> tuple[str, str, bool] | None:
    """Return (in-repo path, body, identity_proven) or None. A single-skill
    root alias is NOT identity-proven: it is served but never cached, so a repo
    that later adds the real skill is resolved again (fed1005 R2)."""
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
            return path, body, True

    tree = _tree_paths(repo)
    if not tree:
        return None
    root = bodies.get("SKILL.md")
    if tree == ["SKILL.md"] and root:
        return "SKILL.md", root, False  # single-skill repo: alias id, unproven
    matches = [p for p in tree if p not in bodies and _dir_of(p) == skill_id][:_MAX_TREE_MATCHES]
    if not matches:
        return None
    passing = [
        (p, b) for p, b in zip(matches, _raw_many(repo, matches)) if b and _identity_ok(p, b, skill_id)
    ]
    return (*passing[0], True) if len(passing) == 1 else None


def _flight_lock(key: str) -> threading.Lock:
    return _STRIPES[hash(key) % len(_STRIPES)]


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
            if body and _identity_ok(cached, body, skill_id):
                return f"{RAW_BASE}/{repo}/HEAD/{cached}", body
        found = _locate(repo, skill_id, hint_path)
        if found is None:
            _cache.put(miss_key, True)
            return None
        path, body, proven = found
        if proven:
            _cache.put(hit_key, path)
        _cache.put(miss_key, None)
        return f"{RAW_BASE}/{repo}/HEAD/{path}", body


MAX_SLUG_SPLITS = 8


def _hub_skills_sh_coordinates(candidates: list[tuple[str, str]]) -> tuple[str, str] | None:
    """The ONE candidate the hub snapshot lists with its original slashes
    (``skills-sh/owner/repo/skill``, matched case-insensitively), returned in
    the row's ORIGINAL case; None when zero or several rows match or the DB is
    unavailable. One bounded IN query; no network."""
    try:
        from sqlalchemy import func

        from app.database import SessionLocal
        from app.models import FederationHubSkill as M

        wanted = [f"skills-sh/{repo}/{sid}".lower() for repo, sid in candidates]
        db = SessionLocal()
        try:
            rows = {r[0] for r in db.query(M.identifier).filter(func.lower(M.identifier).in_(wanted)).all()}
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — DB outage → fail closed, never 500
        logger.warning("skills.sh slug disambiguation unavailable", exc_info=True)
        return None
    if len(rows) != 1:
        return None
    parts = rows.pop().split("/")
    if len(parts) != 4:
        return None
    _, owner, repo, skill = parts
    return f"{owner}/{repo}", skill


def resolve_skills_sh_slug(slug: str) -> tuple[str, str] | None:
    """Decode a skills.sh slug (``owner--repo--skillId``: '/' joined with '--')
    and resolve it. GitHub owners cannot contain '--', but repo names and skill
    ids can (17 of 20,000 prod ids contain '---'), so the repo/skill split is
    ambiguous. An unambiguous slug resolves directly. An ambiguous one is NEVER
    guessed (a sole surviving split can still be the wrong repo): the original
    coordinates come from the hub snapshot, which keeps the slashes, or the
    slug fails closed. At most MAX_SLUG_SPLITS candidates, one DB query."""
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
        if len(splits) > MAX_SLUG_SPLITS:
            return None
    if len(splits) == 1:
        return resolve_repo_skill_md(*splits[0])
    coords = _hub_skills_sh_coordinates(splits) if splits else None
    return resolve_repo_skill_md(*coords) if coords else None


def default_branch(repo: str) -> str | None:
    """The repo's default branch (authed API, cached 6 h), or None."""
    parts = (repo or "").split("/")
    if len(parts) != 2 or not all(_segment_ok(p) for p in parts):
        return None
    key = f"gh-default-branch:{repo}"
    cached = _cache.get(key, HIT_TTL_S)
    if cached:
        return cached
    resp = guarded_get(
        f"https://api.github.com/repos/{repo}", timeout=_TIMEOUT_S, headers=github_api_headers()
    )
    try:
        branch = (
            json.loads(resp.text).get("default_branch")
            if resp is not None and resp.status_code == 200
            else None
        )
    except (TypeError, ValueError, AttributeError):
        branch = None
    # A branch with '/' ('release/v2') cannot be told apart from the skill path
    # in a tree URL, so it is treated as "no usable branch" (no skills_cli line).
    if isinstance(branch, str) and _segment_ok(branch):
        _cache.put(key, branch)
        return branch
    return None
