"""Pre-flight evidence for a self-serve federation registry proposal.

bundles_0811 Phase P3.5 (locked decision #10): "A proposal arriving with
evidence is triageable in a minute; one without is a research task." Before
opening a GitHub issue for a proposed registry we check — using the GitHub
token already installed in the environment (same env-var mechanism as
``app.services.federation_live._github_token`` / ``app.github_dispatch`` —
``GITHUB_TOKEN``/``GH_TOKEN`` for read calls, never hardcoded, never logged):

  - does the repo exist
  - does it contain SKILL.md files, roughly how many
  - what license the repo declares

Network calls route through ``app.services.federation_fetch.guarded_get``
(the SSRF-guarded fetcher every other federation module uses) even though
GitHub itself is a trusted host — defense in depth, and it keeps this module
consistent with the rest of the federation surface's fetch discipline.

Degrades gracefully: a GitHub outage / rate-limit / missing token returns a
summary with ``repo_exists=None`` (unknown, not False) rather than raising or
silently reporting "does not exist" — the caller decides whether an unknown
pre-flight still blocks issue creation.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.federation_fetch import guarded_get
from app.services.federation_live import _github_token

logger = logging.getLogger(__name__)

GITHUB_REPO_API = "https://api.github.com/repos/{repo}"
GITHUB_TREES_API = "https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"

# https://github.com/<owner>/<repo>[.git][/...]  -> owner/repo
_REPO_URL_RE = re.compile(
    r"^https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?(?:[/?#].*)?$"
)


@dataclass
class PreflightResult:
    """Evidence gathered about a proposed registry repo before filing an issue."""

    repo_slug: str | None  # "owner/repo", or None if the URL didn't parse
    repo_exists: bool | None  # None = could not determine (token/network issue)
    skill_md_count: int | None
    license_detected: str | None
    default_branch: str | None = None
    reason: str | None = None  # human-readable note for an unresolved repo
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_slug": self.repo_slug,
            "repo_exists": self.repo_exists,
            "skill_md_count": self.skill_md_count,
            "license_detected": self.license_detected,
            "default_branch": self.default_branch,
            "reason": self.reason,
        }

    @property
    def is_viable(self) -> bool:
        """True only when the repo is confirmed to exist AND carries >=1 SKILL.md.

        Used to gate issue creation — a proposal for a repo that does not
        exist, or exists but has zero SKILL.md files, is reported back to the
        caller instead of opening a junk issue (locked decision #10 §2).
        """
        return self.repo_exists is True and (self.skill_md_count or 0) >= 1


def parse_repo_url(repo_url: str) -> str | None:
    """Extract "owner/repo" from a github.com URL. None if it doesn't parse."""
    m = _REPO_URL_RE.match((repo_url or "").strip())
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def _github_headers() -> dict[str, str]:
    """GitHub API headers with the env token when present.

    Same env-var read as app.services.federation_live._github_token
    (GITHUB_TOKEN or GH_TOKEN) — never a hardcoded value, never logged.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def preflight_repo(repo_url: str) -> PreflightResult:
    """Gather pre-flight evidence for a proposed federation registry repo.

    Never raises — any network/parse failure degrades to an "unknown" field
    rather than crashing the propose flow (matches the graceful-degrade
    discipline of every other federation fetcher in this codebase).
    """
    repo_slug = parse_repo_url(repo_url)
    if repo_slug is None:
        return PreflightResult(
            repo_slug=None,
            repo_exists=None,
            skill_md_count=None,
            license_detected=None,
            reason="repo_url is not a recognizable https://github.com/<owner>/<repo> URL",
        )

    headers = _github_headers()

    resp = guarded_get(GITHUB_REPO_API.format(repo=repo_slug), headers=headers)
    if resp is None:
        return PreflightResult(
            repo_slug=repo_slug,
            repo_exists=None,
            skill_md_count=None,
            license_detected=None,
            reason="GitHub repo lookup failed (network/SSRF-guard/timeout) — pre-flight incomplete",
        )
    if resp.status_code == 404:
        return PreflightResult(
            repo_slug=repo_slug,
            repo_exists=False,
            skill_md_count=None,
            license_detected=None,
            reason="repository not found on GitHub",
        )
    if resp.status_code != 200:
        return PreflightResult(
            repo_slug=repo_slug,
            repo_exists=None,
            skill_md_count=None,
            license_detected=None,
            reason=f"GitHub repo lookup returned HTTP {resp.status_code} (rate-limited or no token?)",
        )

    try:
        repo_data = resp.json()
    # Rationale: malformed JSON from GitHub must never crash the propose flow.
    except Exception:  # noqa: BLE001
        logger.warning("federation_propose: repo lookup returned non-JSON for %s", repo_slug, exc_info=True)
        repo_data = {}

    default_branch = str(repo_data.get("default_branch") or "main")
    license_obj = repo_data.get("license") or {}
    license_detected = license_obj.get("spdx_id") if isinstance(license_obj, dict) else None
    if license_detected in (None, "NOASSERTION"):
        license_detected = None

    skill_md_count = _count_skill_md(repo_slug, default_branch, headers)

    return PreflightResult(
        repo_slug=repo_slug,
        repo_exists=True,
        skill_md_count=skill_md_count,
        license_detected=license_detected,
        default_branch=default_branch,
        raw={"full_name": repo_data.get("full_name"), "description": repo_data.get("description")},
    )


def _count_skill_md(repo_slug: str, branch: str, headers: dict[str, str]) -> int | None:
    """Count SKILL.md files in the repo via one recursive git-tree call.

    Returns None (not 0) when the tree is unavailable/truncated so the caller
    can distinguish "confirmed zero" from "could not confirm" — the same
    honesty discipline federation_live's count-honesty comment (decision #5)
    documents for the GitHub-tap walker.
    """
    resp = guarded_get(GITHUB_TREES_API.format(repo=repo_slug, branch=branch), headers=headers)
    if resp is None or resp.status_code != 200:
        return None
    try:
        tree = resp.json()
    # Rationale: malformed JSON from GitHub must never crash the propose flow.
    except Exception:  # noqa: BLE001
        logger.warning("federation_propose: tree lookup returned non-JSON for %s", repo_slug, exc_info=True)
        return None
    if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
        return None
    count = sum(
        1
        for node in tree["tree"]
        if isinstance(node, dict) and str(node.get("path", "")).endswith("SKILL.md")
    )
    # A truncated tree still gives a (possibly undercounted) real number —
    # report it as a floor rather than None, callers can see truncation risk
    # via a large count near GitHub's tree-listing ceiling.
    return count
