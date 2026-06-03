"""Federation source adapters — evergreen_0206 Phase F.

Two live adapters (Adam q4): Hermes Hub + GitHub/OSS. Each maps its source's
catalog schema into the unified ExternalSkill envelope (app/services/federation).
The adapters are pure parsers over a catalog payload — the HTTP fetch is injected
so the mapping logic is unit-testable without network.
"""

from __future__ import annotations

from typing import Any, Callable

from app.services.federation import ExternalSkill, InstallPath, SourceAdapter

# SPDX-ish license identifiers we consider redistributable for fetch-origin.
_REDISTRIBUTABLE_LICENSES = {
    "mit",
    "apache-2.0",
    "apache2",
    "bsd-3-clause",
    "bsd-2-clause",
    "isc",
    "mpl-2.0",
    "unlicense",
    "cc0-1.0",
    "cc-by-4.0",
}


def _is_redistributable(license_id: str | None) -> bool:
    if not license_id:
        # Unknown license → conservative: NOT redistributable (deep-link/block).
        return False
    return license_id.strip().lower() in _REDISTRIBUTABLE_LICENSES


class HermesHubAdapter(SourceAdapter):
    """Maps the Hermes Hub skills catalog into ExternalSkill.

    Hermes Hub (hermes-agent.nousresearch.com/docs/skills) emits a JSON catalog;
    we already have hub-search-* skills producing a unified envelope. Hub skills
    are SKILL.md-based → fetch-origin installable, license from the manifest.
    """

    source_id = "hermes-hub"

    def __init__(self, fetch: Callable[[str], list[dict[str, Any]]] | None = None) -> None:
        self._fetch = fetch or (lambda q: [])

    def _map(self, row: dict[str, Any]) -> ExternalSkill:
        license_id = row.get("license")
        redist = _is_redistributable(license_id)
        return ExternalSkill(
            slug=row["slug"],
            title=row.get("title", row["slug"]),
            source=self.source_id,
            # Hub skills are SKILL.md → fetch-origin when redistributable, else deep-link.
            install_path=InstallPath.FETCH_ORIGIN if redist else InstallPath.DEEP_LINK,
            origin_url=row.get("url", f"https://hermes-agent.nousresearch.com/skills/{row['slug']}"),
            license=license_id,
            redistributable=redist,
            description=row.get("description", ""),
        )

    def search(self, query: str, limit: int = 20) -> list[ExternalSkill]:
        rows = self._fetch(query)[:limit]
        return [self._map(r) for r in rows]

    def resolve(self, slug: str) -> ExternalSkill | None:
        rows = self._fetch(slug)
        for r in rows:
            if r.get("slug") == slug:
                return self._map(r)
        return None


class GitHubOSSAdapter(SourceAdapter):
    """Maps GitHub repo search results into ExternalSkill.

    Mirrors the Hermes GitHubSource adapter pattern. A repo with a recognized
    OSS license + a SKILL.md is fetch-origin installable; a repo with no license
    or a non-redistributable one is deep-link only (never rehosted).
    """

    source_id = "github-oss"

    def __init__(self, fetch: Callable[[str], list[dict[str, Any]]] | None = None) -> None:
        self._fetch = fetch or (lambda q: [])

    def _map(self, row: dict[str, Any]) -> ExternalSkill:
        # GitHub license object: {"spdx_id": "MIT"} or null.
        lic = row.get("license") or {}
        license_id = lic.get("spdx_id") if isinstance(lic, dict) else lic
        redist = _is_redistributable(license_id)
        has_skill_md = bool(row.get("has_skill_md", True))
        if redist and has_skill_md:
            path = InstallPath.FETCH_ORIGIN
        else:
            # No license / not redistributable / no SKILL.md → deep-link only.
            path = InstallPath.DEEP_LINK
        full_name = row.get("full_name", row.get("slug", "unknown/unknown"))
        return ExternalSkill(
            slug=full_name.replace("/", "--"),  # namespaced slug, collision-safe
            title=row.get("name", full_name),
            source=self.source_id,
            install_path=path,
            origin_url=row.get("html_url", f"https://github.com/{full_name}"),
            license=license_id,
            redistributable=redist,
            description=row.get("description") or "",
        )

    def search(self, query: str, limit: int = 20) -> list[ExternalSkill]:
        rows = self._fetch(query)[:limit]
        return [self._map(r) for r in rows]

    def resolve(self, slug: str) -> ExternalSkill | None:
        full = slug.replace("--", "/")
        rows = self._fetch(full)
        for r in rows:
            if r.get("full_name") == full or r.get("slug") == slug:
                return self._map(r)
        return None


# Registry of live adapters (Adam q4 scope).
def get_adapter(source_id: str, fetch: Callable | None = None) -> SourceAdapter | None:
    if source_id == "hermes-hub":
        return HermesHubAdapter(fetch=fetch)
    if source_id == "github-oss":
        return GitHubOSSAdapter(fetch=fetch)
    return None
