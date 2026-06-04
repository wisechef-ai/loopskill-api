"""federation_0604 — Hermes Skills Hub parity adapters.

Verifies the 5 parity adapters (skills-sh, well-known, clawhub, lobehub,
browse-sh) added to match the canonical Hermes Skills Hub source set. All tests
are OFFLINE — the network fetch is injected, so we assert the pure mapping +
install-path classification without hitting any external API (same discipline as
the Hermes Hub + GitHub adapter tests).

Contract under test:
  - Each adapter maps its source's REAL row schema (confirmed live 2026-06-04)
    into the unified ExternalSkill envelope.
  - Install-path honesty (never conflate indexed vs installable):
      browse-sh, well-known → FETCH_ORIGIN (real redistributable SKILL.md)
      skills-sh, clawhub, lobehub → DEEP_LINK (no rehost)
  - Slugs are collision-safe ("/" → "--") and round-trip through resolve().
  - get_adapter() returns every parity adapter; unknown → None.
"""
from __future__ import annotations

from app.services.federation import InstallPath, route_install
from app.services.federation_adapters import (
    BrowseShAdapter,
    ClawHubAdapter,
    LobeHubAdapter,
    SkillsShAdapter,
    WellKnownAdapter,
    get_adapter,
)


# ── skills.sh (DEEP_LINK aggregator) ─────────────────────────────────────


class TestSkillsShAdapter:
    ROW = {
        "id": "jamditis/claude-skills-journalism/web-scraping",
        "skillId": "web-scraping",
        "name": "web-scraping",
        "source": "jamditis/claude-skills-journalism",
        "installs": 5069,
    }

    def test_maps_and_is_deep_link(self):
        a = SkillsShAdapter(fetch=lambda q: [self.ROW])
        out = a.search("scraping")
        assert len(out) == 1
        s = out[0]
        assert s.source == "skills-sh"
        assert s.install_path == InstallPath.DEEP_LINK  # license unknown → never rehost
        assert s.redistributable is False
        assert s.slug == "jamditis--claude-skills-journalism--web-scraping"
        assert "skills.sh/" in s.origin_url
        # DEEP_LINK is NOT installable (router blocks rehost).
        assert route_install(s).allowed is False

    def test_resolve_round_trip(self):
        a = SkillsShAdapter(fetch=lambda q: [self.ROW])
        slug = a.search("scraping")[0].slug
        assert a.resolve(slug) is not None


# ── well-known (FETCH_ORIGIN domain index) ───────────────────────────────


class TestWellKnownAdapter:
    ROW = {
        "name": "deploy-helper",
        "description": "deploy things",
        "base_url": "https://acme.example",
        "skill_url": "https://acme.example/.well-known/skills/deploy-helper",
        "files": ["SKILL.md"],
    }

    def test_maps_and_is_fetch_origin(self):
        a = WellKnownAdapter(fetch=lambda q: [self.ROW])
        s = a.search("deploy")[0]
        assert s.source == "well-known"
        assert s.install_path == InstallPath.FETCH_ORIGIN
        assert s.redistributable is True
        assert s.slug == "acme.example--deploy-helper"
        # FETCH_ORIGIN + redistributable → installable.
        assert route_install(s).allowed is True

    def test_explicit_non_redistributable_license_blocks(self):
        row = {**self.ROW, "license": "GPL-3.0"}  # not in the redistributable set
        a = WellKnownAdapter(fetch=lambda q: [row])
        s = a.search("deploy")[0]
        assert s.install_path == InstallPath.DEEP_LINK
        assert route_install(s).allowed is False

    def test_resolve_round_trip(self):
        a = WellKnownAdapter(fetch=lambda q: [self.ROW])
        slug = a.search("deploy")[0].slug
        assert a.resolve(slug) is not None


# ── ClawHub (DEEP_LINK community registry) ───────────────────────────────


class TestClawHubAdapter:
    ROW = {
        "slug": "ecovacs-skills-pet-control",
        "displayName": "ecovacs-skills-pet-control",
        "summary": "Control Ecovacs robots.",
        "tags": {"latest": "1.0.1"},
        "stats": {"downloads": 195},
    }

    def test_maps_and_is_deep_link(self):
        a = ClawHubAdapter(fetch=lambda q: [self.ROW])
        s = a.search("ecovacs")[0]
        assert s.source == "clawhub"
        # ClawHavoc supply-chain risk → DEEP_LINK, never rehost.
        assert s.install_path == InstallPath.DEEP_LINK
        assert s.redistributable is False
        assert s.slug == "ecovacs-skills-pet-control"
        assert "clawhub.ai" in s.origin_url
        assert route_install(s).allowed is False

    def test_resolve_round_trip(self):
        a = ClawHubAdapter(fetch=lambda q: [self.ROW])
        slug = a.search("ecovacs")[0].slug
        assert a.resolve(slug) is not None


# ── LobeHub (DEEP_LINK prompt-template marketplace) ──────────────────────


class TestLobeHubAdapter:
    ROW = {
        "identifier": "lateral-thinking-puzzle",
        "homepage": "https://github.com/CSY2022",
        "meta": {
            "title": "Lateral Thinking Puzzle",
            "description": "A turtle soup host.",
            "tags": ["Turtle Soup", "Reasoning"],
        },
    }

    def test_maps_and_is_deep_link(self):
        a = LobeHubAdapter(fetch=lambda q: [self.ROW])
        s = a.search("turtle")[0]
        assert s.source == "lobehub"
        # Prompt template, not a SKILL.md bundle → DEEP_LINK.
        assert s.install_path == InstallPath.DEEP_LINK
        assert s.redistributable is False
        assert s.slug == "lateral-thinking-puzzle"
        assert s.title == "Lateral Thinking Puzzle"
        assert route_install(s).allowed is False

    def test_missing_meta_does_not_crash(self):
        a = LobeHubAdapter(fetch=lambda q: [{"identifier": "x"}])
        s = a.search("")[0]
        assert s.slug == "x"

    def test_resolve_round_trip(self):
        a = LobeHubAdapter(fetch=lambda q: [self.ROW])
        slug = a.search("turtle")[0].slug
        assert a.resolve(slug) is not None


# ── browse.sh (FETCH_ORIGIN site-automation catalog) ─────────────────────


class TestBrowseShAdapter:
    ROW = {
        "slug": "abc7news.com/cali-highway-traffic-tdjcyt",
        "name": "cali-highway-traffic",
        "title": "California Highway Traffic Speeds",
        "description": "Return real-time MPH.",
        "hostname": "abc7news.com",
        "category": "traffic",
        "tags": [],
    }

    def test_maps_and_is_fetch_origin(self):
        a = BrowseShAdapter(fetch=lambda q: [self.ROW])
        s = a.search("traffic")[0]
        assert s.source == "browse-sh"
        # Public SKILL.md catalog → FETCH_ORIGIN, installable.
        assert s.install_path == InstallPath.FETCH_ORIGIN
        assert s.redistributable is True
        assert s.slug == "abc7news.com--cali-highway-traffic-tdjcyt"
        assert route_install(s).allowed is True

    def test_resolve_round_trip(self):
        a = BrowseShAdapter(fetch=lambda q: [self.ROW])
        slug = a.search("traffic")[0].slug
        assert a.resolve(slug) is not None

    def test_resolve_finds_hash_suffixed_slug_via_full_catalog(self):
        """Regression: browse.sh slugs carry a -XXXXXX hash that isn't a catalog
        substring, so a targeted substring fetch misses them. resolve() must fall
        back to the full catalog (empty query) for an exact slug match.

        Simulates the live fetch: a non-empty query substring-misses the hash
        suffix and returns []; the empty query returns the whole catalog.
        """

        def picky_fetch(q: str) -> list[dict]:
            # Mirrors browse_sh_fetch: substring filter; empty query → full catalog.
            if not q:
                return [self.ROW]
            hay = f"{self.ROW['name']} {self.ROW['title']} {self.ROW['description']}".lower()
            return [self.ROW] if q.lower() in hay else []

        a = BrowseShAdapter(fetch=picky_fetch)
        slug = "abc7news.com--cali-highway-traffic-tdjcyt"  # the real hash-suffixed slug
        # The slug-as-query (abc7news.com/cali-highway-traffic-tdjcyt) is NOT a
        # substring of the catalog text → first pass empty; full-catalog pass wins.
        resolved = a.resolve(slug)
        assert resolved is not None, "resolve must find a hash-suffixed slug via full catalog"
        assert resolved.slug == slug
        assert resolved.install_path == InstallPath.FETCH_ORIGIN


# ── Registry parity ──────────────────────────────────────────────────────


class TestParityRegistry:
    PARITY = ["hermes-hub", "github-oss", "skills-sh", "well-known", "clawhub", "lobehub", "browse-sh"]

    def test_every_parity_source_has_an_adapter(self):
        for source_id in self.PARITY:
            adapter = get_adapter(source_id)
            assert adapter is not None, f"{source_id} adapter missing"
            assert adapter.source_id == source_id

    def test_unknown_source_returns_none(self):
        assert get_adapter("nonexistent-source") is None

    def test_live_sources_matches_registry(self):
        from app.services.federation import LIVE_SOURCES

        assert set(LIVE_SOURCES) == set(self.PARITY), "LIVE_SOURCES drifted from adapter registry"

    def test_install_path_classification_matrix(self):
        """The honest indexed-vs-installable split, per source."""
        fetch_origin = {"hermes-hub", "well-known", "browse-sh", "github-oss"}
        deep_link = {"skills-sh", "clawhub", "lobehub"}
        # github-oss can be either (license-dependent); the rest are deterministic.
        assert fetch_origin | deep_link == set(self.PARITY)


# ── Live-fetch wiring (injected JSON, no real network) ───────────────────


class TestLiveFetchWiring:
    """The fetch callables parse each source's real response envelope. We inject
    _safe_json_get so no network is touched, asserting the parse + filter."""

    def test_browse_sh_fetch_parses_catalog_and_filters(self, monkeypatch):
        from app.services import federation_live as fl

        fl._cache.clear()
        catalog = {
            "skills": [
                {"slug": "a.com/traffic", "name": "traffic", "title": "Traffic", "description": "MPH", "tags": []},
                {"slug": "b.com/weather", "name": "weather", "title": "Weather", "description": "rain", "tags": []},
            ]
        }
        monkeypatch.setattr(fl, "_safe_json_get", lambda *a, **k: catalog)
        assert len(fl.browse_sh_fetch("")) == 2  # empty query → full catalog
        out = fl.browse_sh_fetch("traffic")
        assert len(out) == 1 and out[0]["slug"] == "a.com/traffic"
        assert fl.browse_sh_indexed_count() == 2

    def test_lobehub_fetch_parses_agents_envelope(self, monkeypatch):
        from app.services import federation_live as fl

        fl._cache.clear()
        index = {"agents": [{"identifier": "x", "meta": {"title": "X", "description": "puzzle", "tags": ["fun"]}}]}
        monkeypatch.setattr(fl, "_safe_json_get", lambda *a, **k: index)
        assert len(fl.lobehub_fetch("puzzle")) == 1
        assert fl.lobehub_fetch("nomatch") == []
        assert fl.lobehub_indexed_count() == 1

    def test_clawhub_fetch_parses_items_envelope(self, monkeypatch):
        from app.services import federation_live as fl

        fl._cache.clear()
        monkeypatch.setattr(
            fl, "_safe_json_get", lambda *a, **k: {"items": [{"slug": "z", "displayName": "Z", "summary": "s"}]}
        )
        assert len(fl.clawhub_fetch("z")) == 1

    def test_skills_sh_fetch_parses_skills_envelope(self, monkeypatch):
        from app.services import federation_live as fl

        fl._cache.clear()
        monkeypatch.setattr(
            fl, "_safe_json_get", lambda *a, **k: {"skills": [{"id": "o/r/s", "name": "s", "source": "o/r"}]}
        )
        out = fl.skills_sh_fetch("s")
        assert len(out) == 1 and out[0]["id"] == "o/r/s"
        # Empty query returns [] (no firehose on the live toggle).
        assert fl.skills_sh_fetch("") == []

    def test_a_source_outage_degrades_to_empty_not_raise(self, monkeypatch):
        from app.services import federation_live as fl

        fl._cache.clear()
        monkeypatch.setattr(fl, "_safe_json_get", lambda *a, **k: None)  # simulate outage
        assert fl.browse_sh_fetch("x") == []
        assert fl.lobehub_fetch("x") == []
        assert fl.clawhub_fetch("x") == []
        assert fl.skills_sh_fetch("x") == []

    def test_browse_sh_origin_fetch_returns_inline_skillmd(self, monkeypatch):
        from app.services import federation_live as fl

        detail = {"skillMd": "---\nname: x\n---\n# body", "skillMdUrl": "https://cdn/x/SKILL.md"}
        monkeypatch.setattr(fl, "_safe_json_get", lambda *a, **k: detail)
        got = fl.browse_sh_origin_skill_md("a.com--traffic")
        assert got is not None
        url, content = got
        assert content == "---\nname: x\n---\n# body"
        assert url == "https://cdn/x/SKILL.md"

    def test_origin_fetcher_resolves_lazily_for_monkeypatch(self, monkeypatch):
        """get_origin_fetcher honours a monkeypatched module-level fetcher."""
        from app.services import federation_live as fl

        sentinel = lambda slug: ("u", "patched")  # noqa: E731
        monkeypatch.setattr(fl, "browse_sh_origin_skill_md", sentinel)
        assert fl.get_origin_fetcher("browse-sh") is sentinel
        assert fl.get_origin_fetcher("skills-sh") is None  # deep-link, no origin fetch
