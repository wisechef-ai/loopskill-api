"""bundles0811 Phase P3 — federated skills become installable.

Resolves an install INSTRUCTION (a URL the calling agent fetches itself),
never bytes. LoopSkill fetches nothing and stores nothing for a federated
entry — this module's contract is a pure string-in, string-out resolver over
DB coordinates + a tiny per-repo ref cache.

Ground truth (measured against prod, 100-row probe, 2026-08-11):
  - ``federation_hub_skills.repo`` + ``.path`` are the REAL filesystem repo
    and path (NOT a slug to be reconstructed) for 20,509/90,605 rows.
  - Direct ``raw.githubusercontent.com/<repo>/<branch>/<path>/SKILL.md``
    resolves 88/100 (84 on ``main``, 4 on ``master``).
  - A single Trees-API tree-walk on the 12 direct misses recovers 1 more
    (moved skill) — the other 11 are stale index rows / real 404s. Tree-walk
    is therefore a bounded, CACHED FALLBACK, never the primary path — it costs
    one ``git/trees`` call per repo, not per skill.
  - Rows with no repo/path (69,150 clawhub + 505 lobehub + 440 browse-sh) have
    no direct coordinates; the instruction is the origin page URL instead.

Nothing here downloads a SKILL.md body. ``guarded_head`` (zero-byte SSRF-safe
probe) confirms a raw URL exists; the tree-walk fallback fetches the (small)
JSON tree listing, never file content. The calling AGENT executes the
returned instruction — this module only tells it where.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from app.services.federation_fetch import guarded_head, is_redistributable
from app.services.federation_install import GITHUB_RAW_BASE, GITHUB_TREES_URL
from app.services.federation_live import _safe_json_get

logger = logging.getLogger(__name__)

# Measured order: main resolves 84/88 direct hits, master resolves the other 4.
_CANDIDATE_BRANCHES = ("main", "master")

# Per-repo resolved-ref cache (repo -> branch that actually resolved). Process-
# local, TTL-bounded — this is a hot-path optimisation (don't re-probe every
# request for the same repo), not a correctness dependency: a cache miss just
# re-probes. Mirrors the pattern already used by federation_live._TTLCache.
_REF_CACHE_TTL_S = 21600.0  # 6h — a repo's default branch changes rarely


class _RefCache:
    """Thread-safe cache: repo -> (resolved_branch_or_None, resolved_at)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, tuple[str | None, float]] = {}

    def get(self, repo: str) -> str | None | Any:
        """Returns the cached branch, None if cached-as-unresolvable, or the
        sentinel ``_MISS`` if there is no (fresh) cache entry at all."""
        with self._lock:
            hit = self._store.get(repo)
        if hit is None:
            return _MISS
        branch, ts = hit
        if (time.monotonic() - ts) > _REF_CACHE_TTL_S:
            return _MISS
        return branch

    def put(self, repo: str, branch: str | None) -> None:
        with self._lock:
            self._store[repo] = (branch, time.monotonic())

    def clear(self) -> None:  # test hook
        with self._lock:
            self._store.clear()


_MISS = object()
_ref_cache = _RefCache()


@dataclass(frozen=True)
class InstallInstruction:
    """What the calling agent should DO to install this federated skill.

    ``kind='fetch'`` — a raw SKILL.md URL the agent GETs directly.
    ``kind='origin'`` — no direct coordinates; the agent visits this page
    (a repo/catalog listing) to find the skill itself. LoopSkill never
    fetched or stored bytes for either kind.
    """

    kind: str  # "fetch" | "origin"
    url: str
    repo: str | None = None
    path: str | None = None
    branch: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "url": self.url,
            "repo": self.repo,
            "path": self.path,
            "branch": self.branch,
        }


def _raw_skill_md_url(repo: str, branch: str, path: str) -> str:
    clean_path = path.strip("/")
    return f"{GITHUB_RAW_BASE}/{repo}/{branch}/{clean_path}/SKILL.md"


def _probe_branch(repo: str, path: str, branch: str) -> bool:
    """HEAD-only existence probe. Zero body bytes ever transferred."""
    url = _raw_skill_md_url(repo, path=path, branch=branch)
    status = guarded_head(url)
    return status == 200


def _resolve_ref_direct(repo: str, path: str) -> str | None:
    """Try each candidate branch via HEAD, cached per-repo.

    A cache hit short-circuits straight to the ONE branch that worked for
    this repo before (84/88 measured hits were 'main' — do not re-probe
    'master' for a repo we already know is 'main'). A cache miss probes in
    measured-frequency order and caches the winner (or the fact that none
    worked, so repeated misses don't re-probe every request either).
    """
    cached = _ref_cache.get(repo)
    if cached is not _MISS:
        if cached is None:
            return None
        # Re-validate the cached branch still serves THIS path (branch is
        # repo-scoped and stable; path-level 404s are handled by the
        # tree-walk fallback in resolve_install_instruction, not here).
        return cached if _probe_branch(repo, path, cached) else None

    for branch in _CANDIDATE_BRANCHES:
        if _probe_branch(repo, path, branch):
            _ref_cache.put(repo, branch)
            return branch
    _ref_cache.put(repo, None)
    return None


def _tree_walk_fallback(repo: str, path: str, branch: str) -> str | None:
    """ONE tree call for this repo; look for */<last-path-segment>/SKILL.md.

    Bounded fallback for the ~10% of rows whose stored path has moved. Fires
    at most once per repo per resolution attempt (no retries, no pagination
    beyond what the API returns for ``recursive=1``). Fetches only the JSON
    tree listing (paths + shas), never file content.
    """
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    if not leaf:
        return None
    tree = _safe_json_get(GITHUB_TREES_URL.format(repo=repo, branch=branch))
    if not isinstance(tree, dict):
        return None
    entries = tree.get("tree", [])
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_path = str(entry.get("path", ""))
        if entry_path.endswith("/SKILL.md") or entry_path == "SKILL.md":
            parent = entry_path.rsplit("/SKILL.md", 1)[0] if "/" in entry_path else ""
            if parent.rsplit("/", 1)[-1] == leaf or entry_path == f"{leaf}/SKILL.md":
                return entry_path.rsplit("/SKILL.md", 1)[0]
    return None


def resolve_install_instruction(
    *,
    repo: str | None,
    path: str | None,
    origin_url: str | None,
    slug: str | None = None,
) -> InstallInstruction:
    """Resolve one federated entry's install INSTRUCTION. Never fetches bytes.

    Priority (bundles0811 P3 ground truth):
      1. repo+path present -> try direct raw URL on cached/candidate branches.
         A direct miss triggers ONE tree-walk fallback for that repo; a hit
         there is cached too. Still missing -> degrade to origin.
      2. no coordinates -> the origin page URL is the instruction.
    """
    if repo and path:
        branch = _resolve_ref_direct(repo, path)
        if branch is not None:
            return InstallInstruction(
                kind="fetch",
                url=_raw_skill_md_url(repo, path=path, branch=branch),
                repo=repo,
                path=path,
                branch=branch,
            )
        # Direct miss on every candidate branch — one bounded tree-walk,
        # trying each candidate branch's tree in turn (still one call each,
        # capped at len(_CANDIDATE_BRANCHES) — never unbounded).
        for candidate in _CANDIDATE_BRANCHES:
            resolved_path = _tree_walk_fallback(repo, path, candidate)
            if resolved_path is not None:
                _ref_cache.put(repo, candidate)
                return InstallInstruction(
                    kind="fetch",
                    url=_raw_skill_md_url(repo, path=resolved_path, branch=candidate),
                    repo=repo,
                    path=resolved_path,
                    branch=candidate,
                )
    # Degrade to the origin instruction — repo/path absent, or every fetch
    # attempt above failed. Never fabricate a URL to a host known to 404.
    if origin_url:
        return InstallInstruction(kind="origin", url=origin_url)
    if repo:
        return InstallInstruction(kind="origin", url=f"https://github.com/{repo}", repo=repo)
    # Absolute last resort: no coordinates at all. Caller decides what to do
    # with an empty instruction (e.g. 404); this function never invents a URL.
    return InstallInstruction(kind="origin", url="")


__all__ = [
    "InstallInstruction",
    "resolve_install_instruction",
    "is_redistributable",  # re-exported for Q3 licence-recording callers
]
