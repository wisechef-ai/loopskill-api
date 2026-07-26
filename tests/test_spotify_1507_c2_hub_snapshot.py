"""spotify_1507 Phase C2 — Hermes Hub snapshot ingest tests.

Covers:
  - app/services/hub_snapshot.py : slug derivation, dedupe, parse, counts,
    installability mapping, origin_url building, fetch failure, bulk upsert
    idempotency, full ingest round-trip.
  - federation_index_cache        : deduped_indexed_count + snapshot freshness.
  - reindex integration           : hermes-hub source routes through snapshot ingest.

All offline (injectable _get / no network). The snapshot fixture is a small
10-row synthetic JSON, NOT the 33 MB real endpoint.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services import hub_snapshot as hs


# ─────────────────────────── Fixtures ──────────────────────────────────────


def _make_snapshot() -> dict:
    """A small 10-row synthetic Hub snapshot for offline tests."""
    return {
        "version": 1,
        "generated_at": "2026-07-14T18:44:13Z",
        "skill_count": 10,
        "skills": [
            # skills.sh — FETCH_ORIGIN (has repo+path)
            {
                "name": "Telegram Bot Builder",
                "description": "Build a telegram bot",
                "source": "skills.sh",
                "identifier": "skills-sh/davila7/claude-code-templates/telegram-bot-builder",
                "trust_level": "community",
                "repo": "davila7/claude-code-templates",
                "path": "telegram-bot-builder",
                "tags": ["telegram", "bot"],
                "extra": {"installs": 42},
            },
            # clawhub — DEEP_LINK (duplicate_of clawhub)
            {
                "name": "Reason CXR",
                "description": "Medical reasoning skill",
                "source": "clawhub",
                "identifier": "nv-reason-cxr",
                "trust_level": "community",
                "repo": "",
                "path": "",
                "tags": ["medical"],
                "extra": {},
            },
            # official — FETCH_ORIGIN
            {
                "name": "Hermes Markdown",
                "description": "Markdown processing for Hermes",
                "source": "official",
                "identifier": "hermes-markdown",
                "trust_level": "builtin",
                "repo": "NousResearch/hermes-agent",
                "path": "skills/hermes-markdown",
                "tags": [],
                "extra": {},
            },
            # github — FETCH_ORIGIN (has repo+path)
            {
                "name": "Code Review",
                "description": "Automated code review",
                "source": "github",
                "identifier": "github/anthropics/claude-code/code-review",
                "trust_level": "trusted",
                "repo": "anthropics/claude-code",
                "path": "code-review",
                "tags": ["review"],
                "extra": {},
            },
            # lobehub — DEEP_LINK
            {
                "name": "Lobe Chat Agent",
                "description": "A chat agent",
                "source": "lobehub",
                "identifier": "lobe-agent-001",
                "trust_level": "community",
                "repo": "",
                "path": "",
                "tags": ["chat"],
                "extra": {},
            },
            # browse-sh — DEEP_LINK
            {
                "name": "Browse Automation",
                "description": "Browser automation skill",
                "source": "browse-sh",
                "identifier": "browse-sh/site-scraper",
                "trust_level": "community",
                "repo": "",
                "path": "",
                "tags": ["automation"],
                "extra": {},
            },
            # claude-marketplace — DEEP_LINK
            {
                "name": "Claude Market Skill",
                "description": "From claude marketplace",
                "source": "claude-marketplace",
                "identifier": "cm-skill-001",
                "trust_level": "community",
                "repo": "",
                "path": "",
                "tags": [],
                "extra": {},
            },
            # second skills.sh — another duplicate
            {
                "name": "PDF Generator",
                "description": "Generate PDFs",
                "source": "skills.sh",
                "identifier": "skills-sh/user/pdf-tools/generator",
                "trust_level": "community",
                "repo": "user/pdf-tools",
                "path": "generator",
                "tags": ["pdf"],
                "extra": {},
            },
            # second clawhub — another duplicate
            {
                "name": "Data Processor",
                "description": "Process data",
                "source": "clawhub",
                "identifier": "data-processor",
                "trust_level": "community",
                "repo": "",
                "path": "",
                "tags": ["data"],
                "extra": {},
            },
            # github without repo — DEEP_LINK fallback
            {
                "name": "Standalone Skill",
                "description": "No repo info",
                "source": "github",
                "identifier": "github/standalone",
                "trust_level": "community",
                "repo": "",
                "path": "",
                "tags": [],
                "extra": {},
            },
        ],
    }


def _write_snapshot_file(tmp_path: Path, data: dict) -> Path:
    """Write snapshot JSON to a temp file (for fetch tests)."""
    p = tmp_path / "snapshot.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


# ─────────────────────────── Slug derivation ───────────────────────────────


class TestSlugDerivation:
    def test_simple_identifier(self):
        assert hs.derive_slug("nv-reason-cxr") == "nv-reason-cxr"

    def test_nested_identifier_sanitized(self):
        slug = hs.derive_slug("skills-sh/davila7/claude-code-templates/telegram-bot-builder")
        assert slug == "skills-sh-davila7-claude-code-templates-telegram-bot-builder"
        assert "/" not in slug

    def test_empty_falls_back_to_name(self):
        assert hs.derive_slug("", "My Skill") == "my-skill"

    def test_empty_both_falls_back_to_unnamed(self):
        assert hs.derive_slug("", "") == "unnamed"

    def test_collapse_separators(self):
        assert hs.derive_slug("a//b//c") == "a-b-c"

    def test_strip_leading_trailing(self):
        assert hs.derive_slug("/foo/") == "foo"


class TestSlugDedup:
    def test_no_duplicates(self):
        assert hs.dedupe_slugs(["a", "b", "c"]) == ["a", "b", "c"]

    def test_collisions_get_numeric_suffix(self):
        result = hs.dedupe_slugs(["a", "a", "a"])
        assert result == ["a", "a-2", "a-3"]

    def test_mixed(self):
        result = hs.dedupe_slugs(["a", "b", "a", "a", "b"])
        assert result == ["a", "b", "a-2", "a-3", "b-2"]

    def test_order_preserved(self):
        result = hs.dedupe_slugs(["x", "y", "x"])
        assert result == ["x", "y", "x-2"]


# ─────────────────────────── Install path mapping ──────────────────────────


class TestInstallPathMapping:
    def test_skills_sh_with_repo_path_is_fetch_origin(self):
        row = {"source": "skills.sh", "repo": "owner/repo", "path": "skill"}
        assert hs.install_path_for_row(row) == hs.InstallPath.FETCH_ORIGIN

    def test_github_with_repo_path_is_fetch_origin(self):
        row = {"source": "github", "repo": "owner/repo", "path": "skill"}
        assert hs.install_path_for_row(row) == hs.InstallPath.FETCH_ORIGIN

    def test_github_without_repo_is_deep_link(self):
        row = {"source": "github", "repo": "", "path": ""}
        assert hs.install_path_for_row(row) == hs.InstallPath.DEEP_LINK

    def test_clawhub_is_deep_link(self):
        row = {"source": "clawhub", "repo": "x", "path": "y"}
        assert hs.install_path_for_row(row) == hs.InstallPath.DEEP_LINK

    def test_official_is_fetch_origin(self):
        row = {"source": "official", "repo": "", "path": ""}
        assert hs.install_path_for_row(row) == hs.InstallPath.FETCH_ORIGIN

    def test_lobehub_is_deep_link(self):
        row = {"source": "lobehub"}
        assert hs.install_path_for_row(row) == hs.InstallPath.DEEP_LINK

    def test_browse_sh_is_deep_link(self):
        row = {"source": "browse-sh"}
        assert hs.install_path_for_row(row) == hs.InstallPath.DEEP_LINK

    def test_claude_marketplace_is_deep_link(self):
        row = {"source": "claude-marketplace"}
        assert hs.install_path_for_row(row) == hs.InstallPath.DEEP_LINK


# ─────────────────────────── Origin URL building ───────────────────────────


class TestOriginUrl:
    def test_skills_sh_with_repo(self):
        row = {"source": "skills.sh", "repo": "owner/repo", "path": "skill"}
        url = hs.origin_url_for_row(row)
        assert "github.com/owner/repo" in url
        assert "tree/main/skill" in url

    def test_clawhub_url(self):
        """issue #139: the bare /skills/<slug> form is a soft-404.

        This assertion previously pinned the BUG — it required the exact URL
        ClawHub 307-redirects to /skills/skills/<slug> (a client-rendered "not
        found" that still answers HTTP 200). Snapshot rows carry no owner
        handle, so ingest now fails safe to the browse page; when an owner IS
        known the URL is owner-scoped. See tests/test_clawhub_owner_scoped_url.py.
        """
        row = {"source": "clawhub", "identifier": "my-skill", "name": "My"}
        url = hs.origin_url_for_row(row)
        assert url == "https://clawhub.ai/skills"
        assert "clawhub.ai/skills/my-skill" not in url

        owned = hs.origin_url_for_row({**row, "owner": "psyb0t"})
        assert owned == "https://clawhub.ai/psyb0t/skills/my-skill"

    def test_official_url(self):
        row = {"source": "official", "name": "hermes-markdown"}
        url = hs.origin_url_for_row(row)
        assert "hermes-agent.nousresearch.com/skills/hermes-markdown" in url

    def test_github_with_repo_only(self):
        row = {"source": "github", "repo": "owner/repo"}
        url = hs.origin_url_for_row(row)
        assert "github.com/owner/repo" in url


# ────────────────── ponytail_0724: resolved_github_id path truth ──────────────
#
# BUG (found 2026-07-24, Adam): the hub snapshot's flat ``path`` field is a
# skill *label*, NOT its real location in the repo. Upstream carries the truth
# in ``resolved_github_id`` = "<owner>/<repo>/<real/path>". We ignored it and
# built ``/tree/main/<path>``, minting a 404 for 16,006 of 90,605 rows (17.7%
# of the corpus; ~80% of everything skills.sh indexes) — every skill living in
# a subdirectory such as ``skills/<name>``.
#
# Reference case: dietrichgebert/ponytail
#   ingested path     : "ponytail"        → /tree/main/ponytail        → 404
#   resolved_github_id: ".../skills/ponytail" → /tree/main/skills/ponytail → 200


class TestResolvedGithubIdPathTruth:
    """``resolved_github_id`` outranks the flat ``path`` label when present."""

    PONYTAIL = {
        "source": "skills.sh",
        "name": "ponytail",
        "repo": "dietrichgebert/ponytail",
        "path": "ponytail",
        "resolved_github_id": "dietrichgebert/ponytail/skills/ponytail",
    }

    def test_real_subdir_path_is_used_not_the_flat_label(self):
        url = hs.origin_url_for_row(self.PONYTAIL)
        assert url == "https://github.com/dietrichgebert/ponytail/tree/main/skills/ponytail"

    def test_flat_label_path_is_never_emitted_when_resolved_id_present(self):
        url = hs.origin_url_for_row(self.PONYTAIL)
        assert not url.endswith("/tree/main/ponytail"), "the 404-minting label leaked"

    def test_resolved_path_helper_extracts_path_after_owner_repo(self):
        assert hs.resolved_repo_path(self.PONYTAIL) == "skills/ponytail"

    def test_deeply_nested_path_is_preserved_whole(self):
        row = {
            "source": "skills.sh",
            "repo": "pproenca/dot-skills",
            "path": "12-factor-app",
            "resolved_github_id": "pproenca/dot-skills/skills/.experimental/12-factor-app",
        }
        assert hs.resolved_repo_path(row) == "skills/.experimental/12-factor-app"
        assert hs.origin_url_for_row(row).endswith("/tree/main/skills/.experimental/12-factor-app")

    def test_falls_back_to_flat_path_when_no_resolved_id(self):
        row = {"source": "skills.sh", "repo": "owner/repo", "path": "skill"}
        assert hs.resolved_repo_path(row) == "skill"
        assert hs.origin_url_for_row(row).endswith("/tree/main/skill")

    def test_ignores_resolved_id_belonging_to_a_different_repo(self):
        """Fail closed: a mismatched owner/repo prefix must not be trusted."""
        row = {
            "source": "skills.sh",
            "repo": "owner/repo",
            "path": "skill",
            "resolved_github_id": "someone-else/other-repo/skills/skill",
        }
        assert hs.resolved_repo_path(row) == "skill"

    def test_repo_prefix_match_is_case_insensitive(self):
        """GitHub owners are case-insensitive; upstream casing varies."""
        row = {
            "source": "skills.sh",
            "repo": "dietrichgebert/ponytail",
            "path": "ponytail",
            "resolved_github_id": "DietrichGebert/ponytail/skills/ponytail",
        }
        assert hs.resolved_repo_path(row) == "skills/ponytail"

    def test_malformed_resolved_id_falls_back_safely(self):
        for bad in ("", "owner/repo", "not-a-path", None, 123, {"x": 1}):
            row = {"source": "skills.sh", "repo": "owner/repo", "path": "skill", "resolved_github_id": bad}
            assert hs.resolved_repo_path(row) == "skill"

    def test_install_path_still_fetch_origin_for_resolved_rows(self):
        assert hs.install_path_for_row(self.PONYTAIL) == hs.InstallPath.FETCH_ORIGIN

    def test_mapped_row_persists_the_resolved_path(self):
        """map_hub_row stores the REAL path (display + any future consumer).

        SCOPE NOTE (R1, Codex MUST-FIX #1): this stores the corrected path on
        the row. It does NOT by itself repair Hub INSTALL resolution — a
        ``source="hermes-hub"`` row is fetched by
        ``federation_live.hermes_origin_skill_md``, which derives its raw URL
        from the SLUG, not from this column. The original commit message
        overclaimed on that point; this fix repairs the user-facing ORIGIN URL
        (16,006 rows), and leaves Hub-install resolution unchanged.
        """
        mapped = hs.map_hub_row(self.PONYTAIL)
        assert mapped["path"] == "skills/ponytail"
        assert mapped["origin_url"].endswith("/tree/main/skills/ponytail")


class TestHostileResolvedGithubId:
    """R1 MUST-FIX #3 — ``resolved_github_id`` is THIRD-PARTY data.

    The Hub snapshot indexes arbitrary public GitHub repos, so these fields are
    attacker-influenced. The first cut validated only the owner/repo prefix and
    trusted the rest, so ``owner/repo/../../../evil`` produced
    ``https://github.com/owner/repo/tree/main/../../../evil`` — a link that
    escapes the tree it claims to point into.
    """

    BASE = {"source": "skills.sh", "repo": "owner/repo", "path": "label"}

    def _row(self, resolved):
        return {**self.BASE, "resolved_github_id": resolved}

    @pytest.mark.parametrize(
        "hostile",
        [
            "owner/repo/../../../evil",
            "owner/repo/..",
            "owner/repo/./x",
            "owner/repo/a/../../../../etc/passwd",
            "owner/repo/a//b",
            "owner/repo//",
            "owner/repo/a\\b",
            "owner/repo/http:/evil.example",
            "owner/repo/x?y=1",
            "owner/repo/x#frag",
            "owner/repo/a\nb",
            "owner/repo/a\tb",
            "owner/repo/ ",
        ],
    )
    def test_hostile_resolved_id_never_reaches_the_url(self, hostile):
        row = self._row(hostile)
        path = hs.resolved_repo_path(row)
        assert path == "label", f"hostile value leaked: {path!r}"
        assert ".." not in hs.origin_url_for_row(row)

    def test_traversal_specifically_cannot_escape_the_repo_tree(self):
        row = self._row("owner/repo/../../../evil")
        assert hs.origin_url_for_row(row) == "https://github.com/owner/repo/tree/main/label"

    def test_overlong_path_is_rejected_so_storage_cannot_truncate_it(self):
        """A path we accept must fit the String(512) column WHOLE.

        Otherwise ``map_hub_row``'s ``[:512]`` cuts mid-component and stores a
        different — but still plausible — location than the URL advertises.
        """
        row = self._row("owner/repo/" + ("a" * 600))
        assert hs.resolved_repo_path(row) == "label"
        assert len(hs.map_hub_row(row)["path"]) <= 512

    def test_path_at_exactly_the_column_limit_is_accepted_whole(self):
        exact = "a" * 512
        row = self._row(f"owner/repo/{exact}")
        assert hs.resolved_repo_path(row) == exact
        assert hs.map_hub_row(row)["path"] == exact

    def test_a_hostile_flat_label_is_also_rejected(self):
        """The fallback rung is validated by the SAME predicate."""
        row = {"source": "skills.sh", "repo": "owner/repo", "path": "../evil"}
        assert hs.resolved_repo_path(row) == ""
        assert hs.origin_url_for_row(row) == "https://github.com/owner/repo"

    def test_malformed_repo_field_is_not_a_valid_prefix(self):
        """``repo`` must be a real ``owner/name`` pair or the guard is moot."""
        for bad_repo in ("owner", "a/b/c", "/", ""):
            row = {
                "source": "skills.sh",
                "repo": bad_repo,
                "path": "label",
                "resolved_github_id": f"{bad_repo}/skills/x",
            }
            assert hs.resolved_repo_path(row) == ""

    def test_a_rejected_row_degrades_to_deep_link_not_a_bad_fetch(self):
        """No validated path → not installable, rather than installable-wrongly."""
        row = {"source": "skills.sh", "repo": "owner/repo", "path": "../evil"}
        assert hs.install_path_for_row(row) == hs.InstallPath.DEEP_LINK

    @pytest.mark.parametrize(
        "legit",
        [
            # npm-scoped dirs are legal + common. An earlier cut of the hardening
            # rejected '@' and regressed 32 REAL rows (ruvnet/ruflo) from a live
            # 200 to a 404 fallback. Verified live 2026-07-24:
            #   /tree/main/v3/@claude-flow/cli/.claude/skills/browser -> 200
            #   /tree/main/browser                                    -> 404
            "v3/@claude-flow/cli/.claude/skills/browser",
            # Dotdirs are legal; only bare '.' / '..' COMPONENTS are traversal.
            "skills/.experimental/12-factor-app",
            ".claude/skills/foo",
            "a.b/c-d_e/f.g",
            "skills/x",
        ],
    )
    def test_legitimate_unusual_paths_are_not_over_rejected(self, legit):
        """Fail-closed must not mean fail-useless — real paths must survive."""
        assert hs.is_safe_repo_subpath(legit) is True
        row = {**self.BASE, "resolved_github_id": f"owner/repo/{legit}"}
        assert hs.resolved_repo_path(row) == legit


class TestHostileRepoIdent:
    """R2 MUST-FIX #1 — ``repo`` is interpolated into the URL, so it is a
    containment boundary too.

    Reproduced escapes before the fix (``path="safe"`` in every case):

        repo="owner?x/repo" -> https://github.com/owner?x/repo/tree/main/safe
        repo="owner#x/repo" -> https://github.com/owner#x/repo/tree/main/safe

    Both TRUNCATE the advertised path into a query/fragment, so the browser
    requests ``github.com/owner`` — not the tree we showed the user.
    """

    @pytest.mark.parametrize(
        "hostile_repo",
        [
            "owner?x/repo",
            "owner#x/repo",
            "owner@evil.example/repo",
            "owner:8080/repo",
            "/owner/repo/",
            "owner//repo",
            "ow ner/repo",
            "owner/repo/extra",
            "owner",
            "owner/",
            "/repo",
            "..//..",
            "own\ner/repo",
            "ówner/repo",  # non-ASCII homoglyph vector
            "owner/-repo",  # leading hyphen
            "owner/repo.",  # trailing dot
            "a" * 300 + "/repo",
        ],
    )
    def test_hostile_repo_never_reaches_a_github_url(self, hostile_repo):
        row = {"source": "skills.sh", "repo": hostile_repo, "path": "safe"}
        assert hs.is_safe_repo_ident(hostile_repo) is False
        assert hs.resolved_repo_path(row) == ""
        # No GitHub URL is minted at all for an unvalidated repo.
        assert not hs.origin_url_for_row(row).startswith("https://github.com/")

    def test_query_char_cannot_truncate_the_advertised_path(self):
        row = {"source": "skills.sh", "repo": "owner?x/repo", "path": "safe"}
        assert "?" not in hs.origin_url_for_row(row)

    def test_an_unsafe_repo_row_is_not_installable(self):
        row = {"source": "skills.sh", "repo": "owner?x/repo", "path": "safe"}
        assert hs.install_path_for_row(row) == hs.InstallPath.DEEP_LINK

    @pytest.mark.parametrize(
        "legit_repo",
        [
            "dietrichgebert/ponytail",
            "ruvnet/ruflo",
            "pproenca/dot-skills",
            "github/awesome-copilot",
            "a.b_c-d/e.f_g-h",
            "Owner123/Repo456",
            # Leading-dot repo names are legitimate and LIVE (HTTP 200). An
            # earlier cut of this rule rejected them and cost 12 real rows.
            "travisjneuman/.claude",
        ],
    )
    def test_real_repos_are_not_over_rejected(self, legit_repo):
        assert hs.is_safe_repo_ident(legit_repo) is True

    def test_percent_encoded_traversal_in_a_path_is_rejected(self):
        """We never decode, so `%2e%2e%2f` must not pass as an opaque component."""
        row = {
            "source": "skills.sh",
            "repo": "owner/repo",
            "path": "label",
            "resolved_github_id": "owner/repo/%2e%2e%2fevil",
        }
        assert hs.resolved_repo_path(row) == "label"

    @pytest.mark.parametrize("bad", ["a\uff0fb", "a\u202eb", "a\u200db", "a\u00a0b"])
    def test_non_ascii_confusables_in_a_path_are_rejected(self, bad):
        """Full-width solidus / bidi override / ZWJ / NBSP are link-spoof vectors."""
        row = {
            "source": "skills.sh",
            "repo": "owner/repo",
            "path": "label",
            "resolved_github_id": f"owner/repo/{bad}",
        }
        assert hs.resolved_repo_path(row) == "label"

    @pytest.mark.parametrize(
        "legit_unicode",
        [
            # CJK directory names are legitimate and LIVE. An earlier cut
            # rejected ALL non-ASCII and broke 12 real rows from
            # vivy-yi/xiaohongshu-skills (200 -> 404 fallback).
            "skills/01-内容创作/copywriting-skills",
            "skills/07-营销推广/brand-operation",
            "skills/análisis/informe",
            "skills/тест/skill",
        ],
    )
    def test_legitimate_non_ascii_paths_are_preserved(self, legit_unicode):
        """Reject CONFUSABLES, not every non-ASCII character."""
        assert hs.is_safe_repo_subpath(legit_unicode) is True
        row = {
            "source": "skills.sh",
            "repo": "owner/repo",
            "path": "label",
            "resolved_github_id": f"owner/repo/{legit_unicode}",
        }
        assert hs.resolved_repo_path(row) == legit_unicode


# ─────────────────────────── Parse + count ─────────────────────────────────


class TestParseSnapshot:
    def test_parse_returns_correct_count(self):
        data = _make_snapshot()
        rows, generated_at, raw_count = hs.parse_snapshot_skills(data)
        assert raw_count == 10
        assert len(rows) == 10
        assert generated_at == "2026-07-14T18:44:13Z"

    def test_parsed_rows_have_unique_slugs(self):
        data = _make_snapshot()
        rows, _, _ = hs.parse_snapshot_skills(data)
        slugs = [r["slug"] for r in rows]
        assert len(slugs) == len(set(slugs)), "slugs must be unique"

    def test_duplicate_of_set_for_directly_indexed_sources(self):
        data = _make_snapshot()
        rows, _, _ = hs.parse_snapshot_skills(data)
        skills_sh_rows = [r for r in rows if r["upstream_source"] == "skills-sh"]
        clawhub_rows = [r for r in rows if r["upstream_source"] == "clawhub"]
        for r in skills_sh_rows:
            assert r["duplicate_of"] == "skills-sh"
        # Inverted topology (review 2026-07-15): clawhub rows are NOT dupes —
        # the hub snapshot OWNS the clawhub count (direct walk = regressed
        # subset); the route total skips the direct clawhub block instead.
        for r in clawhub_rows:
            assert r["duplicate_of"] is None

    def test_non_duplicate_rows_have_null_duplicate_of(self):
        data = _make_snapshot()
        rows, _, _ = hs.parse_snapshot_skills(data)
        official = [r for r in rows if r["upstream_source"] == "official"]
        for r in official:
            assert r["duplicate_of"] is None


class TestComputeCounts:
    def test_indexed_vs_deduped(self):
        data = _make_snapshot()
        rows, _, _ = hs.parse_snapshot_skills(data)
        indexed, deduped = hs.compute_deduped_count(rows)
        assert indexed == 10
        # 2 skills.sh duplicates only (clawhub owned by hub) → deduped = 8
        assert deduped == 8

    def test_installable_count(self):
        data = _make_snapshot()
        rows, _, _ = hs.parse_snapshot_skills(data)
        installable = hs.compute_installable_count(rows)
        # skills.sh (2 with repo+path) + github (1 with repo+path) + official (1) = 4
        assert installable == 4


# ─────────────────────────── Fetch failure ─────────────────────────────────


class TestFetchFailure:
    def test_fetch_returns_none_on_non_200(self):
        class _BadResp:
            status_code = 503
            content = b""

            def iter_bytes(self, **kw):
                yield b""

        assert hs.fetch_snapshot(_get=lambda *a, **kw: _BadResp()) is None

    def test_fetch_returns_none_on_exception(self):
        def _boom(*a, **kw):
            raise ConnectionError("network down")

        assert hs.fetch_snapshot(_get=_boom) is None

    def test_fetch_returns_none_on_bad_json(self):
        class _BadJsonResp:
            status_code = 200
            content = b"not json"

            def iter_bytes(self, **kw):
                yield b"not json"

        assert hs.fetch_snapshot(_get=lambda *a, **kw: _BadJsonResp()) is None

    def test_fetch_returns_none_on_missing_skills_key(self):
        class _NoSkillsResp:
            status_code = 200
            content = json.dumps({"version": 1}).encode()

            def iter_bytes(self, **kw):
                yield self.content

        assert hs.fetch_snapshot(_get=lambda *a, **kw: _NoSkillsResp()) is None


# ─────────────────────────── Bulk upsert ──────────────────────────────────


class TestBulkUpsert:
    def test_upsert_inserts_all_rows(self, db_session):
        data = _make_snapshot()
        rows, _, _ = hs.parse_snapshot_skills(data)
        count = hs.bulk_upsert_skills(db_session, rows, batch_size=3)
        assert count == 10
        from app.models import FederationHubSkill

        db_rows = db_session.query(FederationHubSkill).all()
        assert len(db_rows) == 10

    def test_upsert_is_idempotent(self, db_session):
        data = _make_snapshot()
        rows, _, _ = hs.parse_snapshot_skills(data)
        hs.bulk_upsert_skills(db_session, rows)
        # Second upsert replaces all → same count
        hs.bulk_upsert_skills(db_session, rows)
        from app.models import FederationHubSkill

        db_rows = db_session.query(FederationHubSkill).all()
        assert len(db_rows) == 10

    def test_upsert_replaces_stale_data(self, db_session):
        from app.models import FederationHubSkill

        # Seed a stale row.
        db_session.add(FederationHubSkill(slug="old-stale", title="Old"))
        db_session.flush()

        data = _make_snapshot()
        rows, _, _ = hs.parse_snapshot_skills(data)
        hs.bulk_upsert_skills(db_session, rows)

        # Old row must be gone.
        assert db_session.query(FederationHubSkill).filter_by(slug="old-stale").first() is None
        assert db_session.query(FederationHubSkill).count() == 10


# ─────────────────────────── Full ingest ──────────────────────────────────


class _FakeResp:
    """Minimal fake httpx.Response for snapshot ingest tests."""

    def __init__(self, data: dict):
        self.status_code = 200
        self.content = json.dumps(data).encode()

    def iter_bytes(self, **kw):
        yield self.content


class TestFullIngest:
    def test_ingest_writes_cache_and_rows(self, db_session):
        snapshot = _make_snapshot()
        fake_get = lambda *a, **kw: _FakeResp(snapshot)

        report = hs.ingest_hub_snapshot(db_session, _get=fake_get, commit=False)
        assert report["status"] == "ok"
        assert report["indexed"] == 10
        assert report["deduped"] == 8
        assert report["installable"] == 4

        from app.models import FederationHubSkill

        assert db_session.query(FederationHubSkill).count() == 10

        from app.services import federation_cache as fcache

        block = fcache.read_source_cache(db_session, "hermes-hub")
        assert block is not None
        assert block["indexed"] == 10
        assert block["deduped_indexed"] == 8
        assert block["installable"] == 4
        assert block["snapshot_generated_at"] is not None
        assert block["walked_at"] is not None

    def test_ingest_failure_preserves_previous_cache(self, db_session):
        # First, seed a good cache.
        from app.services import federation_cache as fcache

        fcache.write_source_cache(
            db_session,
            "hermes-hub",
            indexed_count=50_000,
            installable_count=100,
            first_page=[{"slug": "keep"}],
        )

        # Now a failed fetch.
        fake_get = lambda *a, **kw: None  # fetch returns None
        report = hs.ingest_hub_snapshot(db_session, _get=fake_get, commit=False)
        assert report["status"] == "error"

        # Previous cache must be preserved.
        block = fcache.read_source_cache(db_session, "hermes-hub")
        assert block["indexed"] == 50_000
        assert block["last_error"] is not None
        # First page preserved.
        assert fcache.read_first_page(db_session, "hermes-hub") == [{"slug": "keep"}]

    def test_ingest_failure_first_time_sets_null(self, db_session):
        fake_get = lambda *a, **kw: None
        report = hs.ingest_hub_snapshot(db_session, _get=fake_get, commit=False)
        assert report["status"] == "error"

        from app.services import federation_cache as fcache

        block = fcache.read_source_cache(db_session, "hermes-hub")
        assert block["indexed"] is None
        assert block["last_error"] is not None


# ─────────────────────────── First page builder ────────────────────────────


class TestFirstPage:
    def test_first_page_prioritizes_installable(self):
        data = _make_snapshot()
        rows, _, _ = hs.parse_snapshot_skills(data)
        page = hs.build_first_page(rows, cap=3)
        assert len(page) <= 3
        # All should be fetch_origin (prioritised).
        for item in page:
            assert item["install_path"] == "fetch_origin"

    def test_first_page_shape(self):
        data = _make_snapshot()
        rows, _, _ = hs.parse_snapshot_skills(data)
        page = hs.build_first_page(rows, cap=5)
        assert len(page) == 5
        for item in page:
            assert "slug" in item
            assert "title" in item
            assert "source" in item
            assert item["source"] == "hermes-hub"


# ─────────────────────────── Reindex integration ──────────────────────────


class TestReindexIntegration:
    def test_reindex_hermes_hub_routes_through_snapshot(self, db_session, monkeypatch):
        """The reindex driver must route hermes-hub through snapshot ingest."""
        import scripts.federation_reindex as reindex

        snapshot = _make_snapshot()
        fake_get = lambda *a, **kw: _FakeResp(snapshot)

        monkeypatch.setattr(
            "app.services.hub_snapshot.fetch_snapshot",
            lambda *a, **kw: snapshot,
        )

        report = reindex.reindex_source(db_session, "hermes-hub", dry_run=False)
        assert report["status"] == "ok"
        assert report["indexed"] == 10
        assert report.get("deduped") == 8

    def test_reindex_hermes_hub_failure_returns_error(self, db_session, monkeypatch):
        import scripts.federation_reindex as reindex

        monkeypatch.setattr(
            "app.services.hub_snapshot.fetch_snapshot",
            lambda *a, **kw: None,
        )

        report = reindex.reindex_source(db_session, "hermes-hub")
        assert report["status"] == "error"
        assert report["indexed"] is None


# ─────────────────────────── Deduped count in route ────────────────────────


class TestDedupedCountInRoute:
    def test_route_total_uses_deduped_for_hermes_hub(self, db_session, monkeypatch):
        """The external_indexed TOTAL must use deduped count for hermes-hub."""
        from app.services import federation_cache as fcache

        # Seed hermes-hub with raw=100, deduped=80.
        fcache.write_source_cache(
            db_session,
            "hermes-hub",
            indexed_count=100,
            installable_count=10,
        )
        # Set the deduped column directly.
        from app.models import FederationIndexCache

        row = db_session.get(FederationIndexCache, "hermes-hub")
        row.deduped_indexed_count = 80
        db_session.flush()

        # Seed another source so there's something to sum.
        fcache.write_source_cache(db_session, "skills-sh", indexed_count=20, installable_count=20)

        from tests._app_factory import build_test_app
        from fastapi.testclient import TestClient

        app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
        client = TestClient(app)
        body = client.get("/api/skills/external").json()

        # Total should be deduped(80) + skills-sh(20) = 100, NOT 100+20=120.
        assert body["counts"]["external_indexed"] == 100

        # Per-source should expose deduped_indexed.
        hub_block = body["per_source"]["hermes-hub"]
        assert hub_block["deduped_indexed"] == 80
        assert hub_block["indexed"] == 100  # raw count still visible per-source


# ─────────────── review 2026-07-15: normalization + dedupe topology ───────────────


class TestUpstreamNormalization:
    """The LIVE snapshot spells the source "skills.sh" (dot); source ids use
    "skills-sh". Without normalization, every skills.sh row dodges dedupe and
    installability mapping while hyphen-spelled fixtures stay green."""

    def test_dot_spelled_skills_sh_normalizes(self):
        from app.services.hub_snapshot import normalize_upstream

        assert normalize_upstream("skills.sh") == "skills-sh"
        assert normalize_upstream("SKILLS.SH") == "skills-sh"
        assert normalize_upstream(" clawhub ") == "clawhub"
        assert normalize_upstream(None) == ""

    def test_dot_spelled_row_is_marked_duplicate(self):
        from app.services.hub_snapshot import map_hub_row

        row = map_hub_row(
            {
                "name": "x",
                "source": "skills.sh",
                "identifier": "skills-sh/o/r/x",
                "repo": "o/r",
                "path": "x",
            }
        )
        assert row["duplicate_of"] == "skills-sh"
        assert row["install_path"] == "fetch_origin"

    def test_clawhub_rows_are_not_duplicates(self):
        """Inverted topology: the hub snapshot OWNS the clawhub count (the
        direct clawhub cursor-walk is a regressed subset, 5.5k of 62k); the
        route-level total skips the direct clawhub block instead."""
        from app.services.hub_snapshot import map_hub_row

        row = map_hub_row({"name": "y", "source": "clawhub", "identifier": "y"})
        assert row["duplicate_of"] is None
        assert row["install_path"] == "deep_link"

    def test_route_total_skips_direct_clawhub_when_hub_fresh(self):
        """Pin the _count_for_total topology at the unit level: with a fresh
        hub deduped count present, the direct clawhub block contributes None
        to the total; without it, clawhub's raw count flows through."""
        # Mirror of the closure logic in skill_routes.get_external_skills —
        # kept in sync by this test (if the route changes shape, update both).

        def count_for_total(per_source, block, source_id):
            hub_block = per_source.get("hermes-hub") or {}
            hub_dedup = hub_block.get("deduped_indexed")
            hub_fresh = isinstance(hub_dedup, int) and hub_dedup > 0
            if source_id == "hermes-hub" and hub_fresh:
                return hub_dedup
            if source_id == "clawhub" and hub_fresh:
                return None
            val = block.get("indexed")
            return val if isinstance(val, int) else None

        fresh = {
            "hermes-hub": {"indexed": 83772, "deduped_indexed": 63806},
            "clawhub": {"indexed": 5467},
            "skills-sh": {"indexed": 19966},
        }
        total = sum(c for c in (count_for_total(fresh, b, sid) for sid, b in fresh.items()) if c is not None)
        # hub-deduped (83772 - 19966 skills.sh dupes) + skills-sh direct; clawhub skipped.
        assert total == 63806 + 19966

        stale = {
            "hermes-hub": {"indexed": None, "deduped_indexed": None},
            "clawhub": {"indexed": 5467},
            "skills-sh": {"indexed": 19966},
        }
        total_stale = sum(
            c for c in (count_for_total(stale, b, sid) for sid, b in stale.items()) if c is not None
        )
        # No fresh hub snapshot → direct walks carry the total (old behavior).
        assert total_stale == 5467 + 19966


class TestFetchSignatureParity:
    """Prod bug 2026-07-15: fetch_snapshot passed stream=True to guarded_get,
    which only accepts (url, *, timeout, headers) — TypeError on every REAL
    fetch while tests passed via permissive fake getters. This fake enforces
    the real signature so any kwarg drift fails in CI."""

    def test_fetch_uses_only_guarded_get_signature(self):
        import json as _json

        calls = {}

        def strict_get(url, *, timeout=None, headers=None):
            calls["url"] = url
            body = _json.dumps(
                {"version": 1, "generated_at": "2026-07-15T00:00:00+00:00", "skill_count": 0, "skills": []}
            ).encode()

            class _R:
                status_code = 200
                content = body

            return _R()

        from app.services.hub_snapshot import fetch_snapshot

        data = fetch_snapshot("https://example.test/idx.json", _get=strict_get)
        assert data is not None and data["skill_count"] == 0
        assert calls["url"] == "https://example.test/idx.json"
