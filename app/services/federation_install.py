"""Federation install-resolution — per-source origin SKILL.md resolvers.

federation_0604 install-parity (Adam, 2026-06-04 — option A: server-side
resolution, one SSOT every surface/agent reuses).

The Hermes Skills Hub installs EVERY federated source by resolving content from
ORIGIN at install time (`hermes skills install <source>/<id>` → source.fetch()),
never rehosting. This module is our server-side equivalent: one origin resolver
per installable source, returning ``(source_url, content)`` or ``None``.

Posture (matches Hermes):
  - On-demand only — a resolver fires when a user EXPLICITLY installs a specific
    skill, never as a crawl. Cache-fronted, bounded → light on the server.
  - Nothing is persisted / rehosted. Content is streamed from origin.
  - Unknown/absent license → installable + labelled "community · as-is"
    (Hermes community trust level). An EXPLICIT redistribution-forbidding license
    still downgrades to DEEP_LINK via the adapter/router.

Module split (W0.2 pyfile-size discipline, ≤600 lines): the discovery fetchers
(search) live in ``federation_live``; these install resolvers live here. The two
legacy resolvers (hermes-hub, browse-sh) stay in ``federation_live`` and are
re-exported into the registry below so there is ONE ``get_origin_fetcher``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.services.federation_fetch import guarded_get
from app.services.github_taps import GITHUB_FACET_SOURCES as _GITHUB_FACET_SOURCES_FOR_INSTALL
from app.services.federation_live import (
    _safe_json_get,
    browse_sh_origin_skill_md,
    hermes_origin_skill_md,
)

logger = logging.getLogger(__name__)

LOBEHUB_AGENT_URL = "https://chat-agents.lobehub.com/{agent_id}.json"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com"
GITHUB_TREES_URL = "https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"


def well_known_origin_skill_md(slug: str) -> tuple[str, str] | None:
    """well-known FETCH_ORIGIN resolver. The adapter slug is "host--skillname";
    the SKILL.md lives at https://<host>/.well-known/skills/<name>/SKILL.md.

    Reconstruct host + name from the namespaced slug. host may itself contain
    dashes, so we split on the LAST "--" (adapter joins host + "--" + name).
    """
    if "--" not in (slug or ""):
        return None
    host, _, name = slug.rpartition("--")
    host = host.strip().strip("/")
    name = name.strip()
    if not host or not name:
        return None
    raw_url = f"https://{host}/.well-known/skills/{name}/SKILL.md"
    # superset_0606 Phase A: route through the SSRF-guarded fetch. ``host`` is
    # attacker-supplied (it comes from the namespaced slug), so a naive GET could
    # target 169.254.169.254 or a private host. guarded_get fails closed.
    resp = guarded_get(raw_url)
    if resp is not None and resp.status_code == 200 and resp.text.strip():
        return raw_url, resp.text
    return None


def _lobehub_convert_to_skill_md(agent: dict[str, Any]) -> str:
    """Port of Hermes LobeHubSource._convert_to_skill_md — byte-faithful.

    LobeHub agents are system-prompt templates; convert to a SKILL.md whose
    Instructions section IS the agent's systemRole.
    """
    meta = agent.get("meta")
    if not isinstance(meta, dict):
        meta = agent
    identifier = agent.get("identifier", "lobehub-agent")
    title = meta.get("title", identifier)
    description = meta.get("description", "")
    tags = meta.get("tags", [])
    config = agent.get("config") if isinstance(agent.get("config"), dict) else {}
    system_role = config.get("systemRole", "")
    tag_list = tags if isinstance(tags, list) else []
    fm_lines = [
        "---",
        f"name: {identifier}",
        f"description: {description[:500]}",
        "metadata:",
        "  recipes:",
        f"    tags: [{', '.join(str(t) for t in tag_list)}]",
        "  lobehub:",
        "    source: lobehub",
        "---",
    ]
    body_lines = [
        f"# {title}",
        "",
        description,
        "",
        "## Instructions",
        "",
        system_role if system_role else "(No system role defined)",
    ]
    return "\n".join(fm_lines) + "\n\n" + "\n".join(body_lines) + "\n"


def lobehub_origin_skill_md(slug: str) -> tuple[str, str] | None:
    """lobehub FETCH_ORIGIN resolver — fetch the agent JSON and convert its
    systemRole into a SKILL.md (Hermes parity). Slug is the agent identifier."""
    agent_id = (slug or "").replace("--", "/").strip("/")
    if not agent_id:
        return None
    url = LOBEHUB_AGENT_URL.format(agent_id=agent_id)
    agent = _safe_json_get(url)
    if not isinstance(agent, dict):
        return None
    content = _lobehub_convert_to_skill_md(agent)
    return url, content


def clawhub_origin_skill_md(slug: str) -> tuple[str, str] | None:
    """ClawHub origin resolver — DISABLED (superset_0606 decision #6).

    ClawHub is DEEP_LINK only after the ClawHavoc supply-chain incident
    (341 malicious skills, Feb 2026). We never rehost supply-chain-unvetted
    content, so this resolver always returns ``None``. It is intentionally
    retained (rather than deleted) as a defense-in-depth tripwire: even if a
    future caller re-wires ClawHub into the fetch-origin registry, no body is
    ever streamed. The ``slug`` argument is accepted for signature parity.
    """
    _ = slug  # decision #6: never rehost — no origin fetch, ever.
    return None


def skills_sh_origin_skill_md(slug: str) -> tuple[str, str] | None:
    """skills.sh FETCH_ORIGIN resolver.

    A skills.sh id is "owner/repo/skillId" (slug joins it with "--"; decoded
    without guessing by ``github_skill_path.resolve_skills_sh_slug``). The
    in-repo path is found by ``github_skill_path`` — a parallel raw-CDN wave,
    then ONE authed tree walk (fed1005: the old anonymous 2-call walk exhausted
    the 60/h prod quota and 404'd 90% of cards).
    """
    from app.services.github_skill_path import resolve_skills_sh_slug

    return resolve_skills_sh_slug(slug)


def _parse_github_tree_url(url: str) -> tuple[str, str, str] | None:
    """Parse a canonical GitHub tree/blob URL into (repo, branch, path).

    ``https://github.com/<owner>/<repo>/tree/<branch>/<path...>`` →
    (``<owner>/<repo>``, ``<branch>``, ``<path...>``). Also accepts ``/blob/``.
    Returns None for any non-matching URL (so the caller fails closed). Used by
    the Phase F cache-served install path so a facet's raw SKILL.md is fetched
    from the rate-limit-free raw CDN with zero api.github.com calls.
    """
    m = re.match(
        r"^https?://github\.com/([^/]+/[^/]+)/(?:tree|blob)/([^/]+)/(.+?)/?$",
        (url or "").strip(),
    )
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def github_tap_origin_skill_md(slug: str, row: dict | None = None) -> tuple[str, str] | None:
    """superset_0606 Phase C — fetch a GitHub-facet skill's real SKILL.md.

    The slug is namespaced ``github-<facet>--<skillname>``. We recover the tap +
    skill row, then fetch the skill dir's SKILL.md from the raw host through the
    Phase A SSRF guard. Only redistributable skills reach here (the router blocks
    deep-link/source-available BEFORE the fetcher fires), so a fetched body is
    always license-clean.

    superset_0606 Phase F — ``row`` may be supplied by the caller (the install
    endpoint reads it from the cached first_page). When given, we SKIP the live
    Contents-API walk entirely: ``repo``/``branch``/``skill_path`` come from the
    cached row, and the only network call is to ``raw.githubusercontent.com`` —
    a CDN that does NOT count against the 60/hr anon api.github.com budget. This
    is what makes facet install work under the shared-budget prod box. Falls back
    to the live walk only when no row is supplied AND the cache misses.

    Returns ``(raw_url, content)`` or ``None`` (unresolvable / origin outage).
    """
    if row is None:
        facet = slug.split("--", 1)[0]
        from app.services.federation_live import LIVE_FETCH

        fetch = LIVE_FETCH.get(facet)
        if fetch is None:
            return None
        # Find the matching row in the facet's (cached) listing.
        row = next((r for r in fetch("") if str(r.get("slug")) == slug), None)
    if row is None:
        return None
    repo = row.get("repo")
    branch = row.get("branch", "main")
    skill_path = row.get("skill_path")
    # superset_0606 Phase F — when the row came from the cached first_page (an
    # ExternalSkill.to_dict payload), it carries origin_url but NOT the adapter-
    # internal repo/branch/skill_path. Derive them from the origin_url, which is
    # the canonical GitHub tree URL:
    #   https://github.com/<owner>/<repo>/tree/<branch>/<path...>
    # This keeps the install on the raw CDN only — zero api.github.com calls.
    if (not repo or not skill_path) and row.get("origin_url"):
        derived = _parse_github_tree_url(str(row["origin_url"]))
        if derived is not None:
            repo, branch, skill_path = derived
    if not repo or not skill_path:
        return None
    raw_url = f"{GITHUB_RAW_BASE}/{repo}/{branch}/{skill_path}/SKILL.md"
    resp = guarded_get(raw_url)
    if resp is not None and resp.status_code == 200 and resp.text.strip():
        return raw_url, resp.text
    return None


# Map of source_id → (home_module, function_name) for the FETCH_ORIGIN install
# path. Covers EXACTLY the installable sources (Hermes parity). github-oss is
# absent — discovery only until a prod GITHUB_TOKEN lands (code-search gated).
#
# Each fetcher is resolved LAZILY against its HOME module, so monkeypatching the
# function where it's defined (federation_live for the two legacy resolvers,
# this module for the four federation_0604 ones) is honoured by the route. This
# avoids the stale-re-export trap a flat dict-of-refs would create after the
# W0.2 module split.
_ORIGIN_FETCHER_HOMES = {
    "hermes-hub": ("federation_live", "hermes_origin_skill_md"),
    "browse-sh": ("federation_live", "browse_sh_origin_skill_md"),
    "well-known": ("federation_install", "well_known_origin_skill_md"),
    "lobehub": ("federation_install", "lobehub_origin_skill_md"),
    # clawhub is DEEP_LINK only (superset_0606 decision #6 — ClawHavoc): no
    # origin fetcher is wired, so it can never be rehosted via install.
    "skills-sh": ("federation_install", "skills_sh_origin_skill_md"),
}


def get_origin_fetcher(source_id: str):
    """Resolve the origin SKILL.md fetcher for a source, lazily against its home
    module. Lazy resolution means monkeypatching the function where it's defined
    is honoured by the route, and there's one source of truth for which sources
    are fetch-origin-installable.

    superset_0606 Phase C: every GitHub provider facet (``github-anthropic`` …)
    shares ONE origin fetcher (``github_tap_origin_skill_md``) — the per-repo
    install guarantee. Resolved lazily here so test monkeypatching is honoured.
    """
    from app.services.github_taps import TAP_BY_SOURCE

    if source_id in TAP_BY_SOURCE:
        import app.services.federation_install as _fi

        return _fi.github_tap_origin_skill_md
    entry = _ORIGIN_FETCHER_HOMES.get(source_id)
    if entry is None:
        return None
    import importlib

    mod_name, fn_name = entry
    mod = importlib.import_module(f"app.services.{mod_name}")
    return getattr(mod, fn_name, None)


# Backwards-compatible direct mapping (built once). Prefer get_origin_fetcher()
# in the route so test monkeypatching of the underlying function is honoured.
ORIGIN_FETCHERS = {
    "hermes-hub": hermes_origin_skill_md,
    "browse-sh": browse_sh_origin_skill_md,
    "well-known": well_known_origin_skill_md,
    "lobehub": lobehub_origin_skill_md,
    # clawhub deliberately absent — DEEP_LINK only (decision #6, never rehost).
    "skills-sh": skills_sh_origin_skill_md,
}

# superset_0606 Phase C: every GitHub provider facet resolves through the one
# shared tap origin fetcher (the per-repo install guarantee).
for _facet in _GITHUB_FACET_SOURCES_FOR_INSTALL:
    ORIGIN_FETCHERS[_facet] = github_tap_origin_skill_md
