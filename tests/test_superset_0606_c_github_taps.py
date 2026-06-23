"""superset_0606 Phase C — GitHub tap-list adapter + 6 provider facets.

THE BIG STEAL: one parameterized Contents-API reader over a curated tap-list
serves all 6 provider facets (decision #12). License resolves per skill via the
4-step order (decision #13). All offline — the Contents API + raw fetches are
injected via guarded_get / _safe_json_get monkeypatch.

Covers:
  - app/services/github_taps.py        : the locked tap-list + lookups
  - GitHubTapAdapter                   : the pure mapper (one adapter, N facets)
  - federation_live.github_tap_fetch   : the Contents-API walk + license resolve
  - federation_install.github_tap_origin_skill_md : per-repo install guarantee
  - get_adapter / LIVE_SOURCES / ORIGIN_FETCHERS wiring
"""

from __future__ import annotations

import pytest

from app.services import federation_live as fl
from app.services.federation import LIVE_SOURCES, route_install
from app.services.federation_adapters import GitHubTapAdapter, get_adapter
from app.services.github_taps import (
    GITHUB_FACET_SOURCES,
    GITHUB_TAPS,
    TAP_BY_SOURCE,
)


@pytest.fixture(autouse=True)
def _clear_fed_cache():
    """Clear the federation TTL cache before AND after every test (class methods
    don't trigger module-level setup_function, so an autouse fixture is the
    reliable per-test reset)."""
    fl._cache.clear()
    yield
    fl._cache.clear()


# ─────────────────────────────── tap-list ───────────────────────────────────


class TestTapList:
    def test_six_facets_locked(self):
        assert len(GITHUB_TAPS) == 6
        assert set(GITHUB_FACET_SOURCES) == {
            "github-anthropic", "github-openai", "github-huggingface",
            "github-nvidia", "github-gstack", "github-superpowers",
        }

    def test_per_skill_license_repos_have_no_repo_license(self):
        # anthropics + openai resolve license PER SKILL (decision #13 step 1).
        assert TAP_BY_SOURCE["github-anthropic"].repo_license is None
        assert TAP_BY_SOURCE["github-openai"].repo_license is None

    def test_single_license_repos_carry_repo_license(self):
        assert TAP_BY_SOURCE["github-gstack"].repo_license == "MIT"
        assert TAP_BY_SOURCE["github-huggingface"].repo_license == "Apache-2.0"
        assert "CC-BY-4.0" in TAP_BY_SOURCE["github-nvidia"].repo_license

    def test_trust_tiers(self):
        # decision Q2: anthropics + NVIDIA = trusted-source, rest = curated-community.
        assert TAP_BY_SOURCE["github-anthropic"].trust == "trusted-source"
        assert TAP_BY_SOURCE["github-nvidia"].trust == "trusted-source"
        assert TAP_BY_SOURCE["github-gstack"].trust == "curated-community"

    def test_facets_registered_in_live_sources(self):
        for facet in GITHUB_FACET_SOURCES:
            assert facet in LIVE_SOURCES


# ─────────────────────────────── adapter mapper ─────────────────────────────


class TestGitHubTapAdapter:
    def test_redistributable_skill_is_fetch_origin(self):
        rows = [{"slug": "github-gstack--x", "name": "x", "html_url": "https://h",
                 "license": "mit", "redistributable": True}]
        ad = get_adapter("github-gstack", fetch=lambda q: rows)
        assert isinstance(ad, GitHubTapAdapter)
        sk = ad.search("")[0]
        assert sk.install_path.value == "fetch_origin"
        assert sk.source == "github-gstack"
        assert route_install(sk).allowed is True

    def test_source_available_skill_is_deep_link(self):
        # An anthropic docx-style skill with a non-redistributable license.
        rows = [{"slug": "github-anthropic--docx", "name": "docx", "html_url": "https://h",
                 "license": "licenseref-anthropic", "redistributable": False}]
        ad = get_adapter("github-anthropic", fetch=lambda q: rows)
        sk = ad.search("")[0]
        assert sk.install_path.value == "deep_link"
        assert route_install(sk).allowed is False  # never rehosted

    def test_one_adapter_serves_every_facet(self):
        for facet in GITHUB_FACET_SOURCES:
            ad = get_adapter(facet, fetch=lambda q: [])
            assert isinstance(ad, GitHubTapAdapter)
            assert ad.source_id == facet

    def test_resolve_by_slug(self):
        rows = [{"slug": "github-nvidia--cuda", "name": "cuda", "html_url": "h",
                 "license": "apache-2.0", "redistributable": True}]
        ad = get_adapter("github-nvidia", fetch=lambda q: rows)
        assert ad.resolve("github-nvidia--cuda") is not None
        assert ad.resolve("nope") is None


# ─────────────────────────── Contents-API walk ──────────────────────────────


class _Resp:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        return self._json


class TestGitHubTapFetch:
    def test_walk_lists_skill_dirs_and_resolves_repo_license(self, monkeypatch):
        # gstack: whole-repo MIT → all skills fetch-origin, no per-skill LICENSE read.
        contents = [
            {"type": "dir", "name": "skill-a", "html_url": "https://github.com/garrytan/gstack/tree/main/skill-a"},
            {"type": "dir", "name": "skill-b", "html_url": "https://x"},
            {"type": "dir", "name": ".hidden"},  # skipped
            {"type": "file", "name": "README.md"},  # skipped
        ]

        def fake_json(url, **k):
            if "/contents/" in url:
                return contents
            return {"default_branch": "main"}  # repo metadata

        monkeypatch.setattr(fl, "_safe_json_get", fake_json)
        rows = fl.github_tap_fetch("github-gstack")("")
        assert len(rows) == 2  # .hidden + README skipped
        assert {r["name"] for r in rows} == {"skill-a", "skill-b"}
        # Whole-repo MIT → all redistributable, no per-skill license fetch needed.
        assert all(r["redistributable"] is True for r in rows)
        assert all(r["license"] == "mit" for r in rows)
        assert all(r["slug"].startswith("github-gstack--") for r in rows)

    def test_walk_resolves_per_skill_license_for_mixed_repo(self, monkeypatch):
        # anthropics: per-skill LICENSE.txt. One redistributable (MIT), one not.
        contents = [
            {"type": "dir", "name": "good", "html_url": "h1"},
            {"type": "dir", "name": "docx", "html_url": "h2"},
        ]

        def fake_json(url, **k):
            if "/contents/" in url:
                return contents
            return {"default_branch": "main"}

        def fake_guarded(url, **k):
            # good/LICENSE.txt → MIT (redistributable); docx/LICENSE.txt → proprietary.
            if "good/LICENSE.txt" in url:
                return _Resp(200, text="MIT License\n\nCopyright...")
            if "docx/LICENSE.txt" in url:
                return _Resp(200, text="LicenseRef-Anthropic-Commercial\nAll rights reserved")
            return _Resp(404, text="")

        monkeypatch.setattr(fl, "_safe_json_get", fake_json)
        monkeypatch.setattr(fl, "guarded_get", fake_guarded)
        rows = fl.github_tap_fetch("github-anthropic")("")
        by_name = {r["name"]: r for r in rows}
        assert by_name["good"]["redistributable"] is True
        assert by_name["docx"]["redistributable"] is False  # source-available → deep-link

    def test_token_absent_degrades_graceful_empty(self, monkeypatch):
        # No dir listing (rate-limit / no token on a private read) → [] not crash.
        monkeypatch.setattr(fl, "_safe_json_get", lambda *a, **k: None)
        rows = fl.github_tap_fetch("github-huggingface")("")
        assert rows == []

    def test_query_filters_cached_listing(self, monkeypatch):
        contents = [
            {"type": "dir", "name": "alpha", "html_url": "h"},
            {"type": "dir", "name": "beta", "html_url": "h"},
        ]

        def fake_json(url, **k):
            if "/contents/" in url:
                return contents
            return {"default_branch": "main"}

        monkeypatch.setattr(fl, "_safe_json_get", fake_json)
        fetch = fl.github_tap_fetch("github-gstack")
        assert len(fetch("")) == 2
        assert len(fetch("alpha")) == 1

    def test_github_headers_include_token_when_present(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "x-tok")
        headers = fl._github_headers()
        assert headers["Authorization"] == "Bearer x-tok"
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        assert "Authorization" not in fl._github_headers()

    def test_license_sniff_handles_apache_with_blank_first_line(self):
        # Live-caught bug: anthropic LICENSE.txt leads with a blank line then
        # "Apache License / Version 2.0". Reading line 1 missed it; scanning the
        # header finds apache-2.0 (redistributable).
        apache_body = "\n\n                                 Apache License\n                           Version 2.0, January 2004\n"
        assert fl._sniff_license_name(apache_body) == "apache-2.0"

    def test_license_sniff_recognises_families(self):
        assert fl._sniff_license_name("MIT License\n\nCopyright (c) 2026") == "mit"
        assert fl._sniff_license_name("GNU GENERAL PUBLIC LICENSE\nVersion 3") == "gpl-3.0"
        assert fl._sniff_license_name("totally unknown text") is None

    def test_skill_dir_filter_excludes_non_skill_dirs(self, monkeypatch):
        # Live-caught: gstack 'agents' dir holds only openai.yaml (no SKILL.md) —
        # it must NOT be counted as an indexed skill (count-honesty, decision #5).
        contents = [
            {"type": "dir", "name": "autoplan", "html_url": "h"},
            {"type": "dir", "name": "agents", "html_url": "h"},  # no SKILL.md
        ]
        tree = {
            "truncated": False,
            "tree": [
                {"path": "autoplan/SKILL.md"},
                {"path": "agents/openai.yaml"},  # not a skill
            ],
        }

        def fake_json(url, **k):
            if "/git/trees/" in url:
                return tree
            if "/contents/" in url:
                return contents
            return {"default_branch": "main"}

        monkeypatch.setattr(fl, "_safe_json_get", fake_json)
        rows = fl.github_tap_fetch("github-gstack")("")
        names = {r["name"] for r in rows}
        assert names == {"autoplan"}, "non-skill dir 'agents' must be excluded from the count"


# ───────────────────── per-repo install guarantee ───────────────────────────


class TestTapInstall:
    def test_origin_fetcher_resolves_real_skill_md(self, monkeypatch):
        import app.services.federation_install as fi

        rows = [{"slug": "github-gstack--cool", "name": "cool", "license": "mit",
                 "redistributable": True, "repo": "garrytan/gstack", "branch": "main",
                 "skill_path": "cool", "html_url": "h"}]
        monkeypatch.setitem(fl.LIVE_FETCH, "github-gstack", lambda q: rows)
        monkeypatch.setattr(
            fi, "guarded_get",
            lambda url, **k: _Resp(200, text="---\nname: cool\n---\n# Cool skill") if "SKILL.md" in url else _Resp(404),
        )
        got = fi.github_tap_origin_skill_md("github-gstack--cool")
        assert got is not None
        url, content = got
        assert "raw.githubusercontent.com/garrytan/gstack/main/cool/SKILL.md" in url
        assert "# Cool skill" in content

    def test_unknown_slug_returns_none(self, monkeypatch):
        import app.services.federation_install as fi

        monkeypatch.setitem(fl.LIVE_FETCH, "github-gstack", lambda q: [])
        assert fi.github_tap_origin_skill_md("github-gstack--missing") is None

    def test_every_facet_has_origin_fetcher(self):
        import app.services.federation_install as fi

        for facet in GITHUB_FACET_SOURCES:
            assert fi.get_origin_fetcher(facet) is not None
            assert facet in fi.ORIGIN_FETCHERS

    def test_per_repo_install_guarantee_redistributable_facets(self, monkeypatch):
        """decision #13 hard gate: a redistributable skill from EVERY single-license
        facet (gstack/hf/nvidia/superpowers) resolves a real SKILL.md."""
        import app.services.federation_install as fi
        from app.services.bundle_external import resolve_external_install

        for facet, repo, lic in [
            ("github-gstack", "garrytan/gstack", "mit"),
            ("github-huggingface", "huggingface/skills", "apache-2.0"),
            ("github-nvidia", "NVIDIA/skills", "apache-2.0 and cc-by-4.0"),
            ("github-superpowers", "obra/superpowers", "mit"),
        ]:
            slug = f"{facet}--demo"
            rows = [{"slug": slug, "name": "demo", "license": lic, "redistributable": True,
                     "repo": repo, "branch": "main", "skill_path": "skills/demo", "html_url": "h"}]
            monkeypatch.setitem(fl.LIVE_FETCH, facet, lambda q, _r=rows: _r)
            monkeypatch.setattr(
                fi, "guarded_get",
                lambda url, **k: _Resp(200, text=f"---\nname: demo\n---\n# {url}") if "SKILL.md" in url else _Resp(404),
            )
            payload = resolve_external_install(facet, slug)
            assert payload is not None, f"{facet} must yield an installable skill"
            assert "# " in payload["content"]
            assert payload["install_path"] == "fetch_origin"

    def test_source_available_facet_skill_deep_links(self, monkeypatch):
        """A source-available skill (anthropic docx) must NOT resolve a body —
        it deep-links, installable=false."""
        from app.services.bundle_external import resolve_external_install

        slug = "github-anthropic--docx"
        rows = [{"slug": slug, "name": "docx", "license": "licenseref-anthropic",
                 "redistributable": False, "repo": "anthropics/skills", "branch": "main",
                 "skill_path": "skills/docx", "html_url": "h"}]
        monkeypatch.setitem(fl.LIVE_FETCH, "github-anthropic", lambda q: rows)
        # route_install blocks deep-link BEFORE the fetcher fires → None (no rehost).
        assert resolve_external_install("github-anthropic", slug) is None
