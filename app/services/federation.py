"""Federation install router + source adapters — evergreen_0206 Phase F.

The funnel half of the control plane: make ~88k external skills installable (or
honestly deep-linkable) through one uniform surface, so agents discover Recipes
via the external catalog (SEO / agent-discovery) and convert on the maintenance
moat (B-E).

Three install paths (be honest about installable-vs-indexed):
  1. OSS / SKILL.md / git  → fetch-from-origin, license preserved
  2. MCP server            → register server config (not a file install)
  3. proprietary / locked  → deep-link only (no rehost)

Two adapters ship live this sprint (scope-honest — router + 2 adapters now,
remaining adapters are a thin follow-on):
  - Hermes Hub  (the reference catalog; unified envelope)
  - GitHub/OSS  (the redistributable bulk)

SECURITY — the isolation namespace wall (Adam directive 2026-06-03):
External skills live in a SEPARATE `source` namespace, labeled second-class,
behind a free toggle (off by default). Crucially, the federation surface NEVER
surfaces our internal/private skills (is_public=false, owner-scoped) to any
caller — the quality-namespace wall is also a tenant-isolation wall. See
reconcile-contract §7.3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class InstallPath(str, Enum):
    """How a given external skill can be brought into a cookbook."""

    FETCH_ORIGIN = "fetch_origin"  # OSS/SKILL.md/git — rehost-safe, license preserved
    REGISTER_MCP = "register_mcp"  # MCP server — register config, no file install
    DEEP_LINK = "deep_link"  # proprietary/locked — link only, never rehost


# Internal source namespace — NEVER federated, NEVER surfaced as external.
INTERNAL_SOURCE = "recipes"

# Live adapters — Hermes Skills Hub parity (federation_0604, 2026-06-04).
# Order mirrors the Hermes source router's display order. github-oss is live but
# stays DARK until a GITHUB_TOKEN lands in prod (graceful-empty otherwise) — it
# is wired last per Adam's call. The other six surface real results today.
LIVE_SOURCES = (
    "hermes-hub",
    "skills-sh",
    "well-known",
    "clawhub",
    "lobehub",
    "browse-sh",
    "github-oss",
)


@dataclass(frozen=True)
class ExternalSkill:
    """A skill discovered through a federation source adapter.

    `source` is the namespace label (e.g. 'hermes-hub', 'github-oss'). It is
    ALWAYS second-class relative to internal `recipes` skills and is only shown
    when the caller has the free-source toggle on.
    """

    slug: str
    title: str
    source: str
    install_path: InstallPath
    origin_url: str
    license: str | None = None
    redistributable: bool = True
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "source": self.source,
            "install_path": self.install_path.value,
            "origin_url": self.origin_url,
            "license": self.license,
            "redistributable": self.redistributable,
            "description": self.description,
            "namespace": "external",
            "quality": "community · as-is",
        }


class SourceAdapter:
    """Per-source adapter interface: catalog endpoint + schema map + license."""

    source_id: str = "abstract"

    def __init__(self, fetch: object | None = None) -> None:  # pragma: no cover
        # Concrete adapters accept an injected fetch callable; the base just
        # declares the parameter so the registry can construct any adapter
        # uniformly (federation_0604).
        self._fetch = fetch

    def search(self, query: str, limit: int = 20) -> list[ExternalSkill]:  # pragma: no cover
        raise NotImplementedError

    def resolve(self, slug: str) -> ExternalSkill | None:  # pragma: no cover
        raise NotImplementedError


@dataclass
class InstallDecision:
    """The router's decision for installing one external skill."""

    allowed: bool
    path: InstallPath | None
    reason: str
    skill: ExternalSkill | None = None


def route_install(skill: ExternalSkill) -> InstallDecision:
    """Decide how (or whether) to install an external skill.

    - DEEP_LINK skills are never rehosted — installable=False, link only.
    - FETCH_ORIGIN requires a redistributable license; a license that forbids
      redistribution BLOCKS the install (premortem #10 — license/ToS safety).
    - REGISTER_MCP registers a server config (always allowed; no rehost).
    """
    if skill.install_path == InstallPath.DEEP_LINK:
        return InstallDecision(
            allowed=False,
            path=InstallPath.DEEP_LINK,
            reason="proprietary/locked — deep-link only, never rehosted",
            skill=skill,
        )
    if skill.install_path == InstallPath.FETCH_ORIGIN and not skill.redistributable:
        return InstallDecision(
            allowed=False,
            path=None,
            reason=f"license '{skill.license}' forbids redistribution — install blocked",
            skill=skill,
        )
    return InstallDecision(allowed=True, path=skill.install_path, reason="installable", skill=skill)


@dataclass
class FederatedSearchResult:
    """A merged search across internal + (toggled) external sources."""

    internal: list[dict] = field(default_factory=list)
    external: list[dict] = field(default_factory=list)
    # Honest, NEVER-conflated counts.
    internal_count: int = 0
    external_indexed_count: int = 0
    external_installable_count: int = 0

    def to_dict(self) -> dict:
        return {
            "internal": self.internal,
            "external": self.external,
            "counts": {
                "internal": self.internal_count,
                "external_indexed": self.external_indexed_count,
                "external_installable": self.external_installable_count,
            },
        }


def merge_search(
    internal: list[dict],
    external: list[ExternalSkill],
    *,
    free_sources_enabled: bool,
) -> FederatedSearchResult:
    """Merge internal + external results, honoring the free-source toggle.

    THE ISOLATION WALL: external skills are only included when
    free_sources_enabled is True. Internal results are passed through as-is — but
    the CALLER is responsible for having already filtered internal to the
    caller's own visibility (public + owned); this function never UPgrades
    visibility. External never mixes into the internal list.
    """
    result = FederatedSearchResult()
    result.internal = list(internal)
    result.internal_count = len(internal)

    # Indexed count reflects everything discovered; installable excludes deep-link
    # and non-redistributable. Counts are stated separately, never conflated.
    result.external_indexed_count = len(external)
    installable = [s for s in external if route_install(s).allowed]
    result.external_installable_count = len(installable)

    if free_sources_enabled:
        result.external = [s.to_dict() for s in external]
    else:
        # Toggle off (default): curated stays clean — no external rows leak.
        result.external = []

    return result
