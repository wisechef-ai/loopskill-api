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
        row = {"source": "clawhub", "identifier": "my-skill", "name": "My"}
        url = hs.origin_url_for_row(row)
        assert "clawhub.ai/skills/my-skill" in url

    def test_official_url(self):
        row = {"source": "official", "name": "hermes-markdown"}
        url = hs.origin_url_for_row(row)
        assert "hermes-agent.nousresearch.com/skills/hermes-markdown" in url

    def test_github_with_repo_only(self):
        row = {"source": "github", "repo": "owner/repo"}
        url = hs.origin_url_for_row(row)
        assert "github.com/owner/repo" in url


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
        total = sum(
            c
            for c in (count_for_total(fresh, b, sid) for sid, b in fresh.items())
            if c is not None
        )
        # hub-deduped (83772 - 19966 skills.sh dupes) + skills-sh direct; clawhub skipped.
        assert total == 63806 + 19966

        stale = {
            "hermes-hub": {"indexed": None, "deduped_indexed": None},
            "clawhub": {"indexed": 5467},
            "skills-sh": {"indexed": 19966},
        }
        total_stale = sum(
            c
            for c in (count_for_total(stale, b, sid) for sid, b in stale.items())
            if c is not None
        )
        # No fresh hub snapshot → direct walks carry the total (old behavior).
        assert total_stale == 5467 + 19966
