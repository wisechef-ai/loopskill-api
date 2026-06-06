"""Federation source adapters — evergreen_0206 Phase F.

Two live adapters (Adam q4): Hermes Hub + GitHub/OSS. Each maps its source's
catalog schema into the unified ExternalSkill envelope (app/services/federation).
The adapters are pure parsers over a catalog payload — the HTTP fetch is injected
so the mapping logic is unit-testable without network.
"""

from __future__ import annotations

from typing import Any, Callable

from app.services.federation import ExternalSkill, InstallPath, SourceAdapter
from app.services.federation_fetch import is_redistributable as _canonical_is_redistributable


def _is_redistributable(license_id: str | None) -> bool:
    """Whether a license permits redistribution (fetch-origin install gate).

    superset_0606 Phase A: delegates to the ONE canonical license gate in
    ``federation_fetch`` (decision #13) so there is a single redistributable-set
    SSOT. The canonical gate additionally handles compound declarations like
    NVIDIA's "Apache-2.0 AND CC-BY-4.0" (decision #12) which a plain set-membership
    check would wrongly reject. Unknown / absent / source-available → False.
    """
    return _canonical_is_redistributable(license_id)


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


# ─────────────────────────────────────────────────────────────────────────────
# federation_0604 — Hermes Skills Hub parity adapters.
#
# The canonical Hermes Skills Hub federates these sources (hermes_cli source
# router, verified 2026-06-04): official(=hermes-hub), skills-sh, well-known,
# github, clawhub, lobehub, browse-sh. We already shipped hermes-hub + github-oss
# (Phase F/F2). These five close parity. Each is a PURE PARSER over its source's
# real catalog schema (confirmed live); the network fetch is injected so mapping
# is unit-testable offline (same discipline as Hermes Hub + GitHub adapters).
#
# Install-path semantics (federation_0604 install-parity — Hermes Skills Hub
# installs EVERY source by resolving content from origin at install time; we
# match that. NEVER rehosted — content is fetched on explicit user install):
#   - browse-sh   → FETCH_ORIGIN: public per-skill SKILL.md (Browserbase).
#   - well-known  → FETCH_ORIGIN: GET https://<host>/.well-known/skills/<n>/SKILL.md.
#   - skills-sh   → FETCH_ORIGIN: resolves the underlying public GH repo's SKILL.md
#                   via the anon trees API + raw host (TOKEN-FREE; only code-search
#                   needs a token, which is github-oss, not this).
#   - clawhub     → FETCH_ORIGIN: downloads the version ZIP, extracts SKILL.md.
#                   community · as-is (ClawHavoc history → labelled, not blocked,
#                   matching Hermes's community trust level).
#   - lobehub     → FETCH_ORIGIN: fetches the agent JSON, converts systemRole →
#                   SKILL.md (port of Hermes _convert_to_skill_md).
# An EXPLICIT redistribution-forbidding license still downgrades to DEEP_LINK
# (the GitHubOSSAdapter path); unknown/absent license is installable + labelled
# "community · as-is", exactly as Hermes treats community sources.
# ─────────────────────────────────────────────────────────────────────────────


class SkillsShAdapter(SourceAdapter):
    """skills.sh aggregator. Row schema (from /api/search):
    {id, skillId, name, source(repo 'owner/repo'), installs}.
    DEEP_LINK — skills.sh indexes arbitrary GitHub repos; license is unknown at
    index time, so we link to the canonical page rather than rehost.
    """

    source_id = "skills-sh"

    def __init__(self, fetch: Callable[[str], list[dict[str, Any]]] | None = None) -> None:
        self._fetch = fetch or (lambda q: [])

    def _map(self, row: dict[str, Any]) -> ExternalSkill:
        ident = row.get("id") or row.get("skillId") or row.get("name", "")
        name = row.get("name") or row.get("skillId") or ident
        repo = row.get("source", "")
        return ExternalSkill(
            slug=str(ident).replace("/", "--"),
            title=str(name),
            source=self.source_id,
            # Hermes-parity: installable (resolves to the underlying public GH
            # repo's SKILL.md at install time, token-free). community · as-is.
            install_path=InstallPath.FETCH_ORIGIN,
            origin_url=f"https://skills.sh/{ident}",
            license=None,
            redistributable=True,
            description=f"From {repo}" if repo else "",
        )

    def search(self, query: str, limit: int = 20) -> list[ExternalSkill]:
        rows = self._fetch(query)[:limit]
        return [self._map(r) for r in rows]

    def resolve(self, slug: str) -> ExternalSkill | None:
        ident = slug.replace("--", "/")
        # Two-pass: query by the full id, then by the leaf skill name (skills.sh
        # search is token-based), exact-matching the id in either result set.
        leaf = ident.rsplit("/", 1)[-1]
        for rows in (self._fetch(ident), self._fetch(leaf)):
            for r in rows:
                rid = str(r.get("id") or r.get("skillId") or r.get("name", ""))
                if rid == ident or rid.replace("/", "--") == slug:
                    return self._map(r)
        return None


class WellKnownAdapter(SourceAdapter):
    """A domain exposing /.well-known/skills/index.json. Row schema:
    {name, description, files:[...], base_url, index_url}.
    FETCH_ORIGIN — the index points at real redistributable SKILL.md files; we
    fetch them from origin (license declared per skill, default permissive).
    """

    source_id = "well-known"

    def __init__(self, fetch: Callable[[str], list[dict[str, Any]]] | None = None) -> None:
        self._fetch = fetch or (lambda q: [])

    def _map(self, row: dict[str, Any]) -> ExternalSkill:
        name = row.get("name", "")
        base_url = (row.get("base_url") or "").rstrip("/")
        # Collision-safe slug: host + skill name (a domain's catalog is its namespace).
        host = base_url.split("://", 1)[-1].replace("/", "-") if base_url else "well-known"
        license_id = row.get("license")
        # Default: a site publishing a public well-known skill index intends it
        # to be installed; treat declared-or-absent as redistributable here, but
        # honour an explicit non-redistributable license if present.
        redist = True if not license_id else _is_redistributable(license_id)
        return ExternalSkill(
            slug=f"{host}--{name}".replace("/", "--"),
            title=name,
            source=self.source_id,
            install_path=InstallPath.FETCH_ORIGIN if redist else InstallPath.DEEP_LINK,
            origin_url=row.get("skill_url") or f"{base_url}/.well-known/skills/{name}",
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
            if self._map(r).slug == slug:
                return self._map(r)
        return None


class ClawHubAdapter(SourceAdapter):
    """ClawHub (clawhub.ai/api/v1). Row schema (from /skills):
    {slug, displayName, summary, tags:{latest:..}, stats:{downloads,..}}.
    DEEP_LINK — ClawHavoc supply-chain incident (341 malicious skills, Feb 2026);
    we index + link only, never rehost, always second-class.

    superset_0606 decision #6: ClawHub is DEEP_LINK ONLY. The pre-existing
    code-vs-doc drift (the docstring said DEEP_LINK while the row mapped to
    FETCH_ORIGIN) is resolved IN FAVOUR OF SAFETY. Supply-chain-unvetted content
    is browse/discover only — never fetch-origin, never rehosted. The acceptance
    gate asserts zero rehost.
    """

    source_id = "clawhub"

    def __init__(self, fetch: Callable[[str], list[dict[str, Any]]] | None = None) -> None:
        self._fetch = fetch or (lambda q: [])

    def _map(self, row: dict[str, Any]) -> ExternalSkill:
        slug = row.get("slug") or row.get("displayName", "")
        return ExternalSkill(
            slug=str(slug).replace("/", "--"),
            title=row.get("displayName") or str(slug),
            source=self.source_id,
            # decision #6: DEEP_LINK only — never rehost ClawHavoc-exposed content.
            install_path=InstallPath.DEEP_LINK,
            origin_url=f"https://clawhub.ai/skills/{slug}",
            license=None,
            redistributable=False,
            description=row.get("summary", ""),
        )

    def search(self, query: str, limit: int = 20) -> list[ExternalSkill]:
        rows = self._fetch(query)[:limit]
        return [self._map(r) for r in rows]

    def resolve(self, slug: str) -> ExternalSkill | None:
        ident = slug.replace("--", "/")
        # Two-pass: query by the slug, then by its leaf token (ClawHub search is
        # token-based), matching the exact slug in either result set.
        leaf = ident.rsplit("/", 1)[-1]
        for rows in (self._fetch(ident), self._fetch(leaf)):
            for r in rows:
                rslug = str(r.get("slug") or r.get("displayName", ""))
                if rslug == ident or rslug.replace("/", "--") == slug:
                    return self._map(r)
        return None


class LobeHubAdapter(SourceAdapter):
    """LobeHub agent marketplace (chat-agents.lobehub.com/index.json). Row schema:
    {identifier, homepage, meta:{title, description, tags}}.
    DEEP_LINK — LobeHub agents are system-prompt templates, not SKILL.md bundles;
    surface + link, never auto-install.
    """

    source_id = "lobehub"

    def __init__(self, fetch: Callable[[str], list[dict[str, Any]]] | None = None) -> None:
        self._fetch = fetch or (lambda q: [])

    def _map(self, row: dict[str, Any]) -> ExternalSkill:
        meta = row.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        ident = row.get("identifier") or meta.get("title", "")
        return ExternalSkill(
            slug=str(ident).replace("/", "--"),
            title=meta.get("title") or str(ident),
            source=self.source_id,
            install_path=InstallPath.FETCH_ORIGIN,  # Hermes-parity: prompt→SKILL.md convert at install, community·as-is
            origin_url=row.get("homepage") or f"https://lobehub.com/agent/{ident}",
            license=None,
            redistributable=True,
            description=(meta.get("description") or "")[:200],
        )

    def search(self, query: str, limit: int = 20) -> list[ExternalSkill]:
        rows = self._fetch(query)[:limit]
        return [self._map(r) for r in rows]

    def resolve(self, slug: str) -> ExternalSkill | None:
        ident = slug.replace("--", "/")
        # Two-pass: targeted fetch, then full cached index for exact identifier.
        for rows in (self._fetch(ident), self._fetch("")):
            for r in rows:
                rid = str(r.get("identifier") or "")
                if rid == ident or rid.replace("/", "--") == slug:
                    return self._map(r)
        return None


class BrowseShAdapter(SourceAdapter):
    """browse.sh (Browserbase) site-specific browser-automation skills.
    Row schema (from /api/skills): {slug, name, title, description, hostname,
    category, tags}. FETCH_ORIGIN — public per-skill SKILL.md catalog; content
    resolved via the skill detail endpoint's content URL on install.
    """

    source_id = "browse-sh"

    def __init__(self, fetch: Callable[[str], list[dict[str, Any]]] | None = None) -> None:
        self._fetch = fetch or (lambda q: [])

    def _map(self, row: dict[str, Any]) -> ExternalSkill:
        slug = row.get("slug", "")
        title = row.get("title") or row.get("name") or slug
        return ExternalSkill(
            slug=str(slug).replace("/", "--"),
            title=str(title),
            source=self.source_id,
            install_path=InstallPath.FETCH_ORIGIN,  # public SKILL.md catalog
            origin_url=row.get("sourceUrl") or f"https://browse.sh/skills/{slug}",
            license=row.get("license"),  # usually None → counted, fetched from origin on install
            redistributable=True,
            description=row.get("description", ""),
        )

    def search(self, query: str, limit: int = 20) -> list[ExternalSkill]:
        rows = self._fetch(query)[:limit]
        return [self._map(r) for r in rows]

    def resolve(self, slug: str) -> ExternalSkill | None:
        ident = slug.replace("--", "/")
        # Two-pass: a targeted fetch first (cheap when the source supports query),
        # then the full catalog (empty query) for an exact slug match. browse.sh
        # slugs carry a -XXXXXX hash suffix that isn't a catalog substring, so the
        # substring search alone misses them — the full-catalog exact match is the
        # correctness path. The catalog is cached, so the second fetch is cheap.
        for rows in (self._fetch(ident), self._fetch("")):
            for r in rows:
                rslug = str(r.get("slug", ""))
                if rslug == ident or rslug.replace("/", "--") == slug:
                    return self._map(r)
        return None


# Registry of live adapters — Hermes Skills Hub parity (federation_0604).
_ADAPTER_CLASSES: dict[str, type[SourceAdapter]] = {
    "hermes-hub": HermesHubAdapter,
    "github-oss": GitHubOSSAdapter,
    "skills-sh": SkillsShAdapter,
    "well-known": WellKnownAdapter,
    "clawhub": ClawHubAdapter,
    "lobehub": LobeHubAdapter,
    "browse-sh": BrowseShAdapter,
}


def get_adapter(source_id: str, fetch: Callable | None = None) -> SourceAdapter | None:
    cls = _ADAPTER_CLASSES.get(source_id)
    if cls is None:
        return None
    return cls(fetch=fetch)
