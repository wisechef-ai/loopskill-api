"""evergreen_0206 Phase F2/F3 — live external-catalog seam.

Covers the network-backed fetch layer (app/services/federation_live) and the
/api/skills/external route. All network is mocked — no live calls in CI
(Mom-test discipline: the cold-path is validated separately, by hand, against
real public surfaces before any "88k" claim ships).
"""

from __future__ import annotations

import app.services.federation_live as fl
from app.services.federation_live import (
    _parse_hermes_catalog,
    github_oss_fetch,
    hermes_hub_fetch,
)

# A trimmed copy of the real Hermes Hub skills-catalog table shape.
_HERMES_HTML = """
<table>
<thead><tr><th>Skill</th><th>Description</th><th>Path</th></tr></thead>
<tbody>
<tr><td>findmy</td><td>Track Apple devices via FindMy</td><td>apple/findmy</td></tr>
<tr><td>arxiv</td><td>Search arXiv papers by keyword</td><td>research/arxiv</td></tr>
<tr><td>whisper</td><td>OpenAI speech recognition</td><td>media/whisper</td></tr>
</tbody>
</table>
"""


def setup_function(_):
    # Each test starts with a clean TTL cache so mocks don't leak across tests.
    fl._cache.clear()


# ─────────────────────────── Hermes catalog parser ──────────────────────


class TestHermesCatalogParser:
    def test_parses_rows_and_skips_header(self):
        rows = _parse_hermes_catalog(_HERMES_HTML)
        assert len(rows) == 3, "header row must be skipped"
        slugs = {r["slug"] for r in rows}
        assert slugs == {"apple--findmy", "research--arxiv", "media--whisper"}

    def test_every_row_is_mit_and_origin_linked(self):
        rows = _parse_hermes_catalog(_HERMES_HTML)
        for r in rows:
            assert r["license"] == "MIT", "whole hermes-agent repo is MIT"
            assert r["url"].startswith(
                "https://hermes-agent.nousresearch.com/docs/user-guide/skills/bundled/"
            )

    def test_malformed_table_yields_empty(self):
        assert _parse_hermes_catalog("<p>no table here</p>") == []


class TestHermesFetchCallable:
    def test_query_filters_catalog(self, monkeypatch):
        monkeypatch.setattr(fl, "_load_hermes_catalog", lambda: _parse_hermes_catalog(_HERMES_HTML))
        hits = hermes_hub_fetch("arxiv")
        assert len(hits) == 1 and hits[0]["slug"] == "research--arxiv"

    def test_empty_query_returns_whole_catalog(self, monkeypatch):
        monkeypatch.setattr(fl, "_load_hermes_catalog", lambda: _parse_hermes_catalog(_HERMES_HTML))
        assert len(hermes_hub_fetch("")) == 3

    def test_fetch_failure_degrades_to_empty(self, monkeypatch):
        # superset_0606 Phase A: the hermes catalog load routes through the
        # SSRF-guarded guarded_get; a guard miss (None) → [] (never raises).
        fl._cache.clear()
        monkeypatch.setattr(fl, "guarded_get", lambda *_a, **_k: None)
        assert hermes_hub_fetch("anything") == []


# ─────────────────────────── GitHub graceful degrade ────────────────────


class TestGitHubOSSFetch:
    def test_no_token_degrades_to_empty(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        assert github_oss_fetch("scraper") == [], "no token → graceful empty, never 401-bubble"

    def test_maps_code_search_items_to_adapter_rows(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "x-test-token")

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "items": [
                        {
                            "repository": {
                                "full_name": "acme/cool-skill",
                                "name": "cool-skill",
                                "license": {"spdx_id": "MIT"},
                                "html_url": "https://github.com/acme/cool-skill",
                                "description": "a cool skill",
                            }
                        },
                        # duplicate repo → deduped
                        {"repository": {"full_name": "acme/cool-skill", "name": "cool-skill"}},
                    ]
                }

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **k):
                return _Resp()

        monkeypatch.setattr(fl.httpx, "Client", _Client)
        rows = github_oss_fetch("cool")
        assert len(rows) == 1, "duplicate repos collapse"
        assert rows[0]["full_name"] == "acme/cool-skill"
        assert rows[0]["has_skill_md"] is True

    def test_api_failure_degrades_to_empty(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "x-test-token")

        def boom(*_a, **_k):
            raise RuntimeError("rate limited")

        monkeypatch.setattr(fl.httpx, "Client", boom)
        assert github_oss_fetch("x") == []


# ─────────────────────────── The /api/skills/external route ─────────────


class TestExternalRoute:
    def _patch_live(self, monkeypatch):
        """Wire both adapters to deterministic in-memory fetches."""
        monkeypatch.setattr(
            fl, "_load_hermes_catalog", lambda: _parse_hermes_catalog(_HERMES_HTML)
        )

    def test_toggle_off_by_default_returns_no_external(self, client, monkeypatch):
        self._patch_live(monkeypatch)
        r = client.get("/api/skills/external")
        assert r.status_code == 200
        body = r.json()
        assert body["external"] == [], "off by default → curated stays clean"
        assert body["enabled_sources"] == []
        # Honest indexed teaser still reported for the cheap cached source.
        assert body["per_source"]["hermes-hub"]["indexed"] == 3
        assert body["per_source"]["hermes-hub"]["enabled"] is False

    def test_toggle_on_hermes_returns_second_class_external(self, client, monkeypatch):
        self._patch_live(monkeypatch)
        r = client.get("/api/skills/external?sources=hermes-hub")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled_sources"] == ["hermes-hub"]
        assert len(body["external"]) == 3
        for row in body["external"]:
            assert row["namespace"] == "external"
            assert row["quality"] == "community · as-is"
            assert row["source"] == "hermes-hub"

    def test_query_filters_enabled_source(self, client, monkeypatch):
        self._patch_live(monkeypatch)
        r = client.get("/api/skills/external?sources=hermes-hub&q=whisper")
        body = r.json()
        assert len(body["external"]) == 1
        assert body["external"][0]["slug"] == "media--whisper"

    def test_counts_indexed_vs_installable_not_conflated(self, client, monkeypatch):
        self._patch_live(monkeypatch)
        r = client.get("/api/skills/external?sources=hermes-hub")
        body = r.json()
        # All 3 hermes rows are MIT → fetch-origin → installable.
        assert body["counts"]["external_indexed"] == 3
        assert body["counts"]["external_installable"] == 3
        assert body["per_source"]["hermes-hub"]["indexed"] == 3
        assert body["per_source"]["hermes-hub"]["installable"] == 3

    def test_github_disabled_when_no_token_even_if_toggled_on(self, client, monkeypatch):
        self._patch_live(monkeypatch)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        r = client.get("/api/skills/external?sources=hermes-hub,github-oss")
        body = r.json()
        # github-oss enabled in the toggle, but degrades to 0 indexed (no token).
        assert "github-oss" in body["enabled_sources"]
        assert body["per_source"]["github-oss"]["indexed"] == 0
        # hermes still carries real results — one dead source never empties the page.
        assert body["per_source"]["hermes-hub"]["indexed"] == 3
        assert len(body["external"]) == 3

    def test_unknown_source_is_ignored(self, client, monkeypatch):
        self._patch_live(monkeypatch)
        # federation_0604: lobehub is now LIVE. A genuinely unknown source is dropped.
        r = client.get("/api/skills/external?sources=nonexistent-source,hermes-hub")
        body = r.json()
        assert body["enabled_sources"] == ["hermes-hub"], "non-live source dropped"

    def test_external_route_not_shadowed_by_slug_route(self, client, monkeypatch):
        """Regression: /skills/external must not resolve to /skills/{slug}."""
        self._patch_live(monkeypatch)
        r = client.get("/api/skills/external")
        # The slug route would 404 ("Skill 'external' not found"); the federation
        # route returns the structured envelope with a per_source block.
        assert r.status_code == 200
        assert "per_source" in r.json()


# ─────────── REAL fetch-origin install (the cold-path closer) ────────────


class TestExternalInstall:
    def test_fetch_origin_returns_real_skill_md(self, client, monkeypatch):
        # Adapter resolves the skill (MIT → fetch-origin), origin fetch returns body.
        monkeypatch.setattr(
            fl,
            "_load_hermes_catalog",
            lambda: [
                {
                    "slug": "research--arxiv",
                    "title": "arxiv",
                    "description": "search arxiv",
                    "url": "https://h/skills/research/arxiv",
                    "license": "MIT",
                }
            ],
        )
        monkeypatch.setattr(
            fl,
            "hermes_origin_skill_md",
            lambda slug: ("https://raw.example/SKILL.md", "# arxiv\nreal body"),
        )
        r = client.get("/api/skills/external/hermes-hub/research--arxiv/install")
        assert r.status_code == 200
        body = r.json()
        assert body["install_path"] == "fetch_origin"
        assert body["license"] == "MIT"
        assert body["content"] == "# arxiv\nreal body"
        assert "curl -fsSL" in body["install_command"]
        assert body["quality"] == "community · as-is"

    def test_unknown_source_404(self, client):
        # federation_0604: use a genuinely unknown source (lobehub is now live).
        r = client.get("/api/skills/external/nonexistent-source/x/install")
        assert r.status_code == 404

    def test_internal_source_refused(self, client):
        r = client.get("/api/skills/external/recipes/some-internal/install")
        assert r.status_code == 404

    def test_unresolvable_slug_404(self, client, monkeypatch):
        monkeypatch.setattr(fl, "_load_hermes_catalog", lambda: [])
        r = client.get("/api/skills/external/hermes-hub/research--nope/install")
        assert r.status_code == 404

    def test_origin_fetch_failure_404_not_500(self, client, monkeypatch):
        monkeypatch.setattr(
            fl,
            "_load_hermes_catalog",
            lambda: [
                {"slug": "research--arxiv", "title": "arxiv", "license": "MIT", "url": "https://h/x"}
            ],
        )
        # Resolves, but origin SKILL.md is unfetchable → honest 404, never fabricated.
        monkeypatch.setattr(fl, "hermes_origin_skill_md", lambda slug: None)
        r = client.get("/api/skills/external/hermes-hub/research--arxiv/install")
        assert r.status_code == 404
