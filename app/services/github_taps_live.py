"""GitHub provider-facet tap reader — superset_0606 Phase C (the big steal).

Extracted from ``federation_live`` to keep that module under the 600-line gate.
ONE Contents-API reader over the curated tap-list (app/services/github_taps.py):
each tap (anthropics/skills, huggingface/skills, …) lists its skill dirs and
resolves each skill's license per decision #13 (skill-dir LICENSE.txt → repo
LICENSE → SKILL.md frontmatter → none). Routed through the Phase A SSRF guard;
auth via the GITHUB_TOKEN/GH_TOKEN env path.

All network goes through ``federation_live`` shared primitives accessed via the
module object (``fl._safe_json_get`` / ``fl.guarded_get`` / ``fl._cache``) so
test monkeypatching of those on ``federation_live`` is honoured here too.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import app.services.federation_live as fl
from app.services.federation_fetch import is_redistributable, resolve_license

logger = logging.getLogger(__name__)

GITHUB_CONTENTS_URL = "https://api.github.com/repos/{repo}/contents/{path}"
GITHUB_RAW_HOST = "https://raw.githubusercontent.com"
_GITHUB_TAP_TTL_S = 86_400.0  # facet catalogs are stable — refresh daily


def _github_headers() -> dict[str, str]:
    """GitHub API headers with the env token when present (Phase A auth path)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = fl._github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _sniff_license_name(text: str) -> str | None:
    """Identify the license family from a full LICENSE.txt body.

    GitHub LICENSE.txt files name the license in their header text (e.g. the
    Apache 2.0 file says "Apache License / Version 2.0"). We scan the first ~40
    lines for a known family marker and return its canonical SPDX id, so the
    downstream redistributability gate (federation_fetch.is_redistributable)
    resolves correctly. Returns None when no family is recognised (→ deep-link).

    This is more robust than reading line 1 — many LICENSE.txt files lead with a
    blank line or boilerplate before naming the license.
    """
    head = "\n".join(text.splitlines()[:40]).lower()
    # Ordered most-specific-first so "apache" doesn't shadow a dual declaration.
    markers = [
        ("apache license", "apache-2.0"),
        ("mit license", "mit"),
        ("bsd 3-clause", "bsd-3-clause"),
        ("bsd 2-clause", "bsd-2-clause"),
        ("redistribution and use in source", "bsd-3-clause"),  # BSD body w/o header
        ("mozilla public license", "mpl-2.0"),
        ("isc license", "isc"),
        ("creative commons attribution", "cc-by-4.0"),
        ("gnu general public license", "gpl-3.0"),  # NOT redistributable for our gate
        ("gnu affero", "agpl-3.0"),
        ("the unlicense", "unlicense"),
    ]
    for marker, spdx in markers:
        if marker in head:
            return spdx
    return None


def _resolve_tap_skill_license(
    repo: str, branch: str, skill_path: str, *, repo_license: str | None
) -> tuple[str | None, bool]:
    """Resolve one skill dir's license per decision #13's 4-step order.

    1. skill-dir LICENSE.txt (per-skill, anthropics/openai)
    2. repo-root LICENSE (the tap's ``repo_license``, when single-licensed)
    3. SKILL.md frontmatter ``license:``
    4. none → (None, False) → deep-link

    Returns ``(license_id, redistributable)``. Network reads (LICENSE.txt / raw
    SKILL.md) go through the SSRF guard and are cheap (cached at the fetch layer).
    """
    timeout = fl._HTTP_TIMEOUT_S

    # Step 1: per-skill LICENSE.txt (raw). Only fetched when the repo is NOT
    # single-licensed (repo_license is None) — single-license repos skip straight
    # to step 2 to save API calls.
    skill_dir_license: str | None = None
    if repo_license is None:
        raw_license_url = f"{GITHUB_RAW_HOST}/{repo}/{branch}/{skill_path}/LICENSE.txt"
        resp = fl.guarded_get(raw_license_url, timeout=timeout)
        if resp is not None and resp.status_code == 200 and resp.text.strip():
            skill_dir_license = _sniff_license_name(resp.text)

    # Step 3 source: SKILL.md frontmatter (only read if steps 1+2 are empty).
    skill_md: str | None = None
    if skill_dir_license is None and repo_license is None:
        raw_md_url = f"{GITHUB_RAW_HOST}/{repo}/{branch}/{skill_path}/SKILL.md"
        resp = fl.guarded_get(raw_md_url, timeout=timeout)
        if resp is not None and resp.status_code == 200:
            skill_md = resp.text

    license_id, redist = resolve_license(
        skill_dir_license=skill_dir_license,
        repo_root_license=repo_license,
        skill_md=skill_md,
    )
    # Defensive: if resolve_license found nothing but the LICENSE.txt head
    # contained a redistributable token we missed, re-check.
    if license_id is None and skill_dir_license:
        license_id, redist = skill_dir_license.strip().lower(), is_redistributable(skill_dir_license)
    return license_id, redist


def _github_default_branch_cached(repo: str) -> str:
    """Resolve (and cache) a repo's default branch via the REST API."""
    cache_key = f"gh-branch:{repo}"
    cached = fl._cache.get(cache_key, _GITHUB_TAP_TTL_S)
    if cached is not None:
        return cached
    data = fl._safe_json_get(f"https://api.github.com/repos/{repo}", headers=_github_headers())
    branch = "main"
    if isinstance(data, dict) and data.get("default_branch"):
        branch = str(data["default_branch"])
    fl._cache.put(cache_key, branch)
    return branch


def _skill_dirs_from_tree(repo: str, branch: str, prefix: str) -> set[str] | None:
    """Return the set of dir names under ``prefix`` that contain a SKILL.md.

    One recursive git-tree call (cheap, cached) lets us count ONLY real skills —
    honest indexed counts (decision #5), not "every directory". Returns None when
    the tree is unavailable/truncated (caller falls back to listing all dirs).
    """
    cache_key = f"gh-skilltree:{repo}:{prefix}"
    cached = fl._cache.get(cache_key, _GITHUB_TAP_TTL_S)
    if cached is not None:
        return set(cached)
    tree = fl._safe_json_get(
        f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1",
        headers=_github_headers(),
    )
    if not isinstance(tree, dict) or tree.get("truncated") or not isinstance(tree.get("tree"), list):
        return None
    pfx = f"{prefix}/" if prefix else ""
    names: set[str] = set()
    for node in tree["tree"]:
        if not isinstance(node, dict):
            continue
        p = str(node.get("path", ""))
        if not p.endswith("/SKILL.md") or not p.startswith(pfx):
            continue
        rel = p[len(pfx) :]
        # The skill dir is the FIRST path component after the prefix.
        first = rel.split("/", 1)[0]
        if first and first != "SKILL.md":
            names.add(first)
    fl._cache.put(cache_key, list(names))
    return names


def _walk_github_tap(tap: Any) -> list[dict[str, Any]]:
    """Walk one tap's skill dirs via the Contents API; resolve license per skill.

    Count-honesty (decision #5): a dir is a SKILL only if it contains a SKILL.md.
    We detect that via one cheap recursive git-tree call so non-skill dirs (e.g.
    gstack's ``agents`` which holds only ``openai.yaml``) are NOT counted as
    indexed skills. If the tree is unavailable, we fall back to listing all dirs.
    """
    repo, path = tap.repo, tap.path
    contents_url = GITHUB_CONTENTS_URL.format(repo=repo, path=path.strip("/"))
    entries = fl._safe_json_get(contents_url, headers=_github_headers())
    if not isinstance(entries, list):
        logger.info("github tap %s: no dir listing (token? rate-limit?) — graceful empty", tap.source_id)
        return []
    branch = _github_default_branch_cached(repo)
    prefix = path.strip("/")
    # Honest skill detection: the set of dirs that actually contain a SKILL.md.
    skill_dirs = _skill_dirs_from_tree(repo, branch, prefix)
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "dir":
            continue
        name = entry.get("name", "")
        if not name or name.startswith((".", "_")):
            continue
        # Skip dirs that don't contain a SKILL.md (when we could determine the
        # tree). This keeps the indexed count honest — only real skills.
        if skill_dirs is not None and name not in skill_dirs:
            continue
        skill_path = f"{prefix}/{name}" if prefix else name
        license_id, redist = _resolve_tap_skill_license(
            repo, branch, skill_path, repo_license=tap.repo_license
        )
        rows.append(
            {
                # namespaced, collision-safe slug: facet--reponame--skillname
                "slug": f"{tap.source_id}--{name}",
                "name": name,
                "description": "",
                "html_url": entry.get("html_url", f"https://github.com/{repo}/tree/{branch}/{skill_path}"),
                "license": license_id,
                "redistributable": redist,
                "repo": repo,
                "skill_path": skill_path,
                "branch": branch,
                "trust": tap.trust,
            }
        )
    return rows


def github_tap_fetch(source_id: str) -> Callable[[str], list[dict[str, Any]]]:
    """Build a fetch callable for a GitHub provider-facet source.

    Returns a closure ``fetch(query) -> rows`` (the adapter's injected fetch).
    The closure walks the tap's dir via the Contents API, resolves each skill's
    license per decision #13, and returns adapter rows:
      {slug, name, description, html_url, license, redistributable}
    Degrades GRACEFULLY to [] on any error (a facet outage never 500s the route).
    """
    from app.services.github_taps import TAP_BY_SOURCE

    tap = TAP_BY_SOURCE.get(source_id)

    def _fetch(query: str) -> list[dict[str, Any]]:
        if tap is None:
            return []
        cache_key = f"github-tap:{source_id}"
        cached = fl._cache.get(cache_key, _GITHUB_TAP_TTL_S)
        if cached is None:
            cached = _walk_github_tap(tap)
            if cached:
                fl._cache.put(cache_key, cached)
        q = (query or "").strip().lower()
        if not q:
            return cached
        return [
            r
            for r in cached
            if q in str(r.get("name", "")).lower() or q in str(r.get("description", "")).lower()
        ]

    return _fetch
