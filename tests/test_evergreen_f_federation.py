"""evergreen_0206 Phase F — federation router + adapters + isolation wall."""

from __future__ import annotations

from app.services.federation import (
    ExternalSkill,
    InstallPath,
    merge_search,
    route_install,
)
from app.services.federation_adapters import (
    GitHubOSSAdapter,
    HermesHubAdapter,
    get_adapter,
)


# ─────────────────────────── Adapters map to unified envelope ────────────


class TestHermesHubAdapter:
    def test_maps_skill_md_to_fetch_origin(self):
        rows = [
            {
                "slug": "agent-rescue",
                "title": "Agent Rescue",
                "license": "MIT",
                "description": "rescue",
                "url": "https://h/skills/agent-rescue",
            }
        ]
        a = HermesHubAdapter(fetch=lambda q: rows)
        skills = a.search("rescue")
        assert len(skills) == 1
        s = skills[0]
        assert s.source == "hermes-hub"
        assert s.install_path == InstallPath.FETCH_ORIGIN
        assert s.redistributable is True
        assert s.license == "MIT"

    def test_unknown_license_becomes_deeplink(self):
        rows = [{"slug": "mystery", "title": "Mystery"}]  # no license
        a = HermesHubAdapter(fetch=lambda q: rows)
        s = a.search("x")[0]
        assert s.redistributable is False
        assert s.install_path == InstallPath.DEEP_LINK


class TestGitHubOSSAdapter:
    def test_mit_repo_with_skill_md_is_installable(self):
        rows = [
            {
                "full_name": "acme/cool-skill",
                "name": "cool-skill",
                "license": {"spdx_id": "MIT"},
                "has_skill_md": True,
                "html_url": "https://github.com/acme/cool-skill",
            }
        ]
        a = GitHubOSSAdapter(fetch=lambda q: rows)
        s = a.search("cool")[0]
        assert s.source == "github-oss"
        assert s.slug == "acme--cool-skill", "slug must be collision-safe namespaced"
        assert s.install_path == InstallPath.FETCH_ORIGIN
        assert s.license == "MIT"

    def test_no_license_repo_is_deeplink(self):
        rows = [{"full_name": "acme/proprietary", "name": "prop", "license": None, "has_skill_md": True}]
        a = GitHubOSSAdapter(fetch=lambda q: rows)
        s = a.search("x")[0]
        assert s.redistributable is False
        assert s.install_path == InstallPath.DEEP_LINK

    def test_gpl_repo_blocks_redistribution(self):
        """A copyleft license not in the redistributable allowlist → deep-link."""
        rows = [
            {
                "full_name": "acme/gpl-tool",
                "name": "gpl",
                "license": {"spdx_id": "GPL-3.0"},
                "has_skill_md": True,
            }
        ]
        a = GitHubOSSAdapter(fetch=lambda q: rows)
        s = a.search("x")[0]
        assert s.redistributable is False
        assert s.install_path == InstallPath.DEEP_LINK


# ─────────────────────────── Install router ─────────────────────────────


class TestInstallRouter:
    def test_redistributable_oss_installs(self):
        s = ExternalSkill(
            "s",
            "S",
            "github-oss",
            InstallPath.FETCH_ORIGIN,
            "https://github.com/x/s",
            license="MIT",
            redistributable=True,
        )
        d = route_install(s)
        assert d.allowed is True
        assert d.path == InstallPath.FETCH_ORIGIN

    def test_non_redistributable_blocked(self):
        s = ExternalSkill(
            "s",
            "S",
            "github-oss",
            InstallPath.FETCH_ORIGIN,
            "https://github.com/x/s",
            license="GPL-3.0",
            redistributable=False,
        )
        d = route_install(s)
        assert d.allowed is False
        assert "forbids redistribution" in d.reason

    def test_deeplink_never_rehosted(self):
        s = ExternalSkill(
            "s",
            "S",
            "vendor",
            InstallPath.DEEP_LINK,
            "https://vendor.com/s",
            license="proprietary",
            redistributable=False,
        )
        d = route_install(s)
        assert d.allowed is False
        assert "deep-link only" in d.reason

    def test_mcp_register_allowed(self):
        s = ExternalSkill("srv", "Server", "mcp", InstallPath.REGISTER_MCP, "https://mcp.example/server")
        d = route_install(s)
        assert d.allowed is True
        assert d.path == InstallPath.REGISTER_MCP


# ─────────────────────────── Free-source toggle ─────────────────────────


class TestFreeSourceToggle:
    def test_toggle_off_hides_external(self):
        internal = [{"slug": "internal-1", "source": "recipes"}]
        external = [
            ExternalSkill(
                "e1", "E1", "github-oss", InstallPath.FETCH_ORIGIN, "https://github.com/x/e1", license="MIT"
            )
        ]
        res = merge_search(internal, external, free_sources_enabled=False)
        assert res.external == [], "toggle off → curated stays clean, no external"
        assert res.internal_count == 1
        # Counts still reported honestly even when hidden.
        assert res.external_indexed_count == 1

    def test_toggle_on_shows_external_second_class(self):
        internal = [{"slug": "internal-1", "source": "recipes"}]
        external = [
            ExternalSkill(
                "e1", "E1", "github-oss", InstallPath.FETCH_ORIGIN, "https://github.com/x/e1", license="MIT"
            )
        ]
        res = merge_search(internal, external, free_sources_enabled=True)
        assert len(res.external) == 1
        assert res.external[0]["namespace"] == "external"
        assert res.external[0]["quality"] == "community · as-is"

    def test_indexed_vs_installable_counts_not_conflated(self):
        external = [
            ExternalSkill(
                "ok",
                "OK",
                "github-oss",
                InstallPath.FETCH_ORIGIN,
                "https://github.com/x/ok",
                license="MIT",
                redistributable=True,
            ),
            ExternalSkill(
                "link", "Link", "vendor", InstallPath.DEEP_LINK, "https://v/link", redistributable=False
            ),
        ]
        res = merge_search([], external, free_sources_enabled=True)
        assert res.external_indexed_count == 2
        assert res.external_installable_count == 1, "deep-link is indexed but NOT installable"


# ─────────────────── THE ISOLATION WALL (Adam directive) ────────────────


class TestIsolationWall:
    def test_external_never_mixes_into_internal_list(self):
        internal = [{"slug": "our-internal", "source": "recipes", "is_public": False}]
        external = [
            ExternalSkill(
                "ext",
                "Ext",
                "github-oss",
                InstallPath.FETCH_ORIGIN,
                "https://github.com/x/ext",
                license="MIT",
            )
        ]
        res = merge_search(internal, external, free_sources_enabled=True)
        # internal and external are SEPARATE lists — never merged.
        internal_slugs = {s["slug"] for s in res.internal}
        external_slugs = {s["slug"] for s in res.external}
        assert internal_slugs.isdisjoint(external_slugs)
        assert "ext" not in internal_slugs
        assert "our-internal" not in external_slugs

    def test_merge_search_does_not_upgrade_internal_visibility(self):
        """merge_search passes internal through as-is — it never adds rows.

        The caller is responsible for pre-filtering internal to public+owned.
        merge_search must never inject internal rows the caller didn't pass.
        """
        res = merge_search([], [], free_sources_enabled=True)
        assert res.internal == []
        assert res.internal_count == 0


# ─────────────────────────── Adapter registry ───────────────────────────


class TestAdapterRegistry:
    def test_get_known_adapters(self):
        assert isinstance(get_adapter("hermes-hub"), HermesHubAdapter)
        assert isinstance(get_adapter("github-oss"), GitHubOSSAdapter)

    def test_unknown_adapter_returns_none(self):
        assert get_adapter("lobehub") is None, "non-live adapters are follow-on"


class TestAdapterResolve:
    def test_hermes_resolve_found(self):
        rows = [{"slug": "agent-rescue", "title": "AR", "license": "MIT"}]
        a = HermesHubAdapter(fetch=lambda q: rows)
        s = a.resolve("agent-rescue")
        assert s is not None and s.slug == "agent-rescue"

    def test_hermes_resolve_missing(self):
        a = HermesHubAdapter(fetch=lambda q: [])
        assert a.resolve("nope") is None

    def test_github_resolve_by_full_name(self):
        rows = [{"full_name": "acme/x", "name": "x", "license": {"spdx_id": "MIT"},
                 "has_skill_md": True}]
        a = GitHubOSSAdapter(fetch=lambda q: rows)
        s = a.resolve("acme--x")
        assert s is not None and s.slug == "acme--x"

    def test_github_resolve_missing(self):
        a = GitHubOSSAdapter(fetch=lambda q: [])
        assert a.resolve("acme--nope") is None
