"""GitHub tap-list: the 6 provider facets + curated github-oss allowlist.

superset_0606 Phase C — THE BIG STEAL. Ported from the Hermes Skills Hub's
``GitHubSource.DEFAULT_TAPS`` (tools/skills_hub.py:395-413): the Hub's "6 provider
facets" are not six integrations — they are ONE GitHub Contents-API reader over a
curated ``{repo, path}`` tap array. We add a tap-list, not six adapters.

decision #12: ``github-oss`` is a CURATED ALLOWLIST, not a code-search firehose.
Every row is license-verified live; the allowlist IS the source.

decision #13: license resolves PER SKILL via the 4-step order
(skill-dir LICENSE.txt → repo LICENSE → SKILL.md frontmatter → none). Mixed-license
repos (anthropics/openai ship a per-skill LICENSE.txt) resolve each skill
independently: redistributable → fetch-origin; source-available → deep-link.

Each tap surfaces as a DISTINCT source id (``github-anthropic``, ``github-openai``,
…) so the facets browse separately, mirroring the Hub's facet UI 1:1.
"""

from __future__ import annotations

from typing import NamedTuple


class GitHubTap(NamedTuple):
    """One curated GitHub tap = a facet source id + its {repo, path} + trust tier.

    ``repo_license`` is the repo-root SPDX when the WHOLE repo is single-licensed
    (gstack MIT, huggingface Apache-2.0, …) — used as license-resolution step 2.
    When None (anthropics/openai), license resolves PER SKILL from each skill
    dir's own LICENSE.txt (step 1).

    ``trust`` (decision Q2): 'trusted-source' (anthropics/NVIDIA) vs
    'curated-community' (the rest). curated-Pro stays the visual headline.
    """

    source_id: str  # e.g. "github-anthropic" — a distinct facet source
    repo: str  # "anthropics/skills"
    path: str  # "skills/" (the dir under which skill dirs live; "" = repo root)
    repo_license: str | None  # repo-root SPDX, or None for per-skill-license repos
    trust: str  # "trusted-source" | "curated-community"


# The locked allowlist (decision #12, our own monorepo removed per Adam).
# Verified live 2026-06-06 (anon Contents API): anthropics/skills=17 dirs,
# huggingface/skills=16, garrytan/gstack=69, NVIDIA Apache-2.0+CC-BY-4.0.
GITHUB_TAPS: tuple[GitHubTap, ...] = (
    # Per-skill license repos (anthropics + openai ship a LICENSE.txt per skill
    # dir; repo_license=None forces per-skill resolution → redistributable subset
    # installs fetch-origin, source-available (docx/pdf/pptx/xlsx) deep-links).
    GitHubTap("github-anthropic", "anthropics/skills", "skills/", None, "trusted-source"),
    GitHubTap("github-openai", "openai/skills", "skills/.curated/", None, "curated-community"),
    # Whole-repo single-license repos → every skill is fetch-origin installable
    # (decision #13 hard gate: each yields >=1 installable skill via Recipes).
    GitHubTap("github-huggingface", "huggingface/skills", "skills/", "Apache-2.0", "curated-community"),
    GitHubTap("github-nvidia", "NVIDIA/skills", "skills/", "Apache-2.0 AND CC-BY-4.0", "trusted-source"),
    GitHubTap("github-gstack", "garrytan/gstack", "", "MIT", "curated-community"),
    GitHubTap("github-superpowers", "obra/superpowers", "skills/", "MIT", "curated-community"),
)

# Fast lookup: source_id -> tap.
TAP_BY_SOURCE: dict[str, GitHubTap] = {t.source_id: t for t in GITHUB_TAPS}

# The facet source ids, in display order (mirrors the Hub facet UI).
GITHUB_FACET_SOURCES: tuple[str, ...] = tuple(t.source_id for t in GITHUB_TAPS)
