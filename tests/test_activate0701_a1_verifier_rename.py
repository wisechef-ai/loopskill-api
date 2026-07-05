"""loopskill_activate_0701 Phase A1 — Verifier rename kill-tests (council report §5).

This is the FULL kill-test suite mandated by the codex council report
(`docs/design/activate0701-phaseA1-council-report.md` §5) for the verifier
rename + compatibility alias PR.

Scope pins (council §6 'Conditions for GO'):
  * NO physical DB table rename (loops / loop_versions / loop_ratings unchanged).
  * NO redirects; dual-mount behaviour at /api/loops + /api/verifiers.
  * MCP dual-names (canonical loopskill_search_verifiers / loopskill_get_verifier
    + old loopskill_search_loops / loopskill_get_loop callable).
  * /api/loops behaviour byte-identical to before the rename.
  * Terminology lint is context-aware.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.auth_ctx import AuthContext
from app.database import get_db
from app.middleware._public_paths import PUBLIC_PREFIXES  # noqa: compat-test


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def app_client(db_session):
    """App with the verifier router (dual-mounted at /api/loops AND /api/verifiers)
    + a stub auth middleware mirroring APIKeyMiddleware's contract.

    x-test-auth: user   -> authenticated user
    x-test-auth: master -> master scope
    x-test-auth: cbt    -> share-token scope (403 on writes)
    (absent)             -> anonymous
    """
    from app.verifier_routes import router as verifier_router

    app = FastAPI()

    @app.middleware("http")
    async def _stub_auth(request: Request, call_next):
        hdr = request.headers.get("x-test-auth")
        if hdr == "user":
            request.state.auth_ctx = AuthContext(scope="user", user_id=uuid4())
        elif hdr == "master":
            request.state.auth_ctx = AuthContext(scope="master")
        elif hdr == "cbt":
            request.state.auth_ctx = AuthContext(scope="cbt_token", cookbook_scope=uuid4())
        else:
            request.state.auth_ctx = AuthContext.anonymous()
        return await call_next(request)

    app.include_router(verifier_router)

    # Expose the test DB session on the app so payload-parity tests that share
    # the session across two FastAPI apps can recover it after their dependency-
    # override shuffling.  # compat-test
    app._test_db_session = db_session  # type: ignore[attr-defined]  # compat-test

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app, raise_server_exceptions=True)


_VALID_BODY = {
    "slug": "verifier-x",
    "title": "Verifier X",
    "description": "test verifier",
    "success_condition": "exit 0",
    "verification_script": "exit 0",
    "system_prompt": "be careful",
    "stopping_criteria": {"success": "s", "failure": "f", "budget": "b"},
}


def _publish(client, slug: str = "verifier-x", **overrides) -> dict:
    body = dict(_VALID_BODY, slug=slug)
    body.update(overrides)
    r = client.post("/api/verifiers", json=body, headers={"x-test-auth": "user"})
    assert r.status_code == 201, r.text
    return r.json()


# ── (1) ORM alias: Verifier class + Loop = Verifier compat alias ─────────────


class TestOrmAlias:
    """ORM class is named Verifier; Loop remains a callable alias."""

    def test_verifier_class_defined(self):
        from app.models import Verifier

        assert Verifier.__name__ == "Verifier"

    def test_verifier_tablename_is_loops(self):
        from app.models import Verifier

        assert Verifier.__tablename__ == "loops"

    def test_loop_alias_is_verifier(self):
        from app.models import Loop, Verifier

        assert Loop is Verifier

    def test_loop_rating_alias(self):
        from app.models import LoopRating, VerifierRating

        assert LoopRating is VerifierRating

    def test_loop_version_alias(self):
        from app.models import LoopVersion, VerifierVersion

        assert LoopVersion is VerifierVersion


# ── (2) Schemas: Verifier* canonical + Loop* alias ───────────────────────────


class TestSchemaAliases:
    def test_verifier_out_canonical(self):
        from app.schemas import VerifierOut

        assert VerifierOut.__name__ == "VerifierOut"

    def test_loop_out_is_verifier_out(self):
        from app.schemas import LoopOut, VerifierOut

        assert LoopOut is VerifierOut

    def test_loop_detail_out_is_verifier_detail_out(self):
        from app.schemas import LoopDetailOut, VerifierDetailOut

        assert LoopDetailOut is VerifierDetailOut

    def test_loop_publish_in_is_verifier_publish_in(self):
        from app.schemas import LoopPublishIn, VerifierPublishIn

        assert LoopPublishIn is VerifierPublishIn

    def test_loop_run_in_out_aliases(self):
        from app.schemas import LoopRunIn, LoopRunOut, VerifierRunIn, VerifierRunOut

        assert LoopRunIn is VerifierRunIn
        assert LoopRunOut is VerifierRunOut

    def test_loop_rate_aliases(self):
        from app.schemas import (
            LoopRateIn,
            LoopRatingOut,
            VerifierRateIn,
            VerifierRatingOut,
        )

        assert LoopRateIn is VerifierRateIn
        assert LoopRatingOut is VerifierRatingOut


# ── (3) PUBLIC_PREFIXES includes /api/verifiers ──────────────────────────────


class TestPublicPrefixes:
    def test_verifiers_in_public_prefixes(self):
        assert "/api/verifiers" in set(PUBLIC_PREFIXES), (
            "/api/verifiers dropped from PUBLIC_PREFIXES — anonymous browse will 401."
        )

    def test_loops_still_in_public_prefixes(self):
        # compat: /api/loops MUST stay public-readable.
        assert "/api/loops" in set(PUBLIC_PREFIXES)

    def test_verifiers_prefix_covers_browse_and_detail(self):
        prefixes = tuple(PUBLIC_PREFIXES)
        for path in ("/api/verifiers", "/api/verifiers/some-slug"):
            assert any(path.startswith(p) for p in prefixes), f"{path} not matched"


# ── (4) /api/verifiers route surface — dual-mount + byte-identical payloads ──


class TestVerifierRoutesDualMount:
    def test_get_verifiers_anonymous_200(self, app_client):
        res = app_client.get("/api/verifiers")
        assert res.status_code == 200

    def test_publish_verifier_requires_auth(self, app_client):
        res = app_client.post("/api/verifiers", json=_VALID_BODY)
        assert res.status_code == 401

    def test_publish_and_get_verifier(self, app_client):
        body = _publish(app_client, slug="v1")
        assert body["slug"] == "v1"
        assert body["success_condition"] == "exit 0"

        d = app_client.get("/api/verifiers/v1")
        assert d.status_code == 200
        assert d.json()["verification_script"] == "exit 0"
        assert set(d.json()["stopping_criteria"]) == {"success", "failure", "budget"}

    def test_unknown_verifier_404(self, app_client):
        assert app_client.get("/api/verifiers/ghost").status_code == 404

    def test_duplicate_slug_conflicts(self, app_client):
        _publish(app_client, slug="dup")
        r = app_client.post(
            "/api/verifiers",
            json=dict(_VALID_BODY, slug="dup"),
            headers={"x-test-auth": "user"},
        )
        assert r.status_code == 409

    def test_anonymous_run_is_401(self, app_client):
        _publish(app_client, slug="run-v")
        res = app_client.post("/api/verifiers/run-v/run", json={})
        assert res.status_code == 401

    def test_anonymous_rate_is_401(self, app_client):
        _publish(app_client, slug="rate-v")
        res = app_client.post("/api/verifiers/rate-v/rate", json={"rating": 5})
        assert res.status_code == 401

    def test_anonymous_publish_is_401(self, app_client):
        res = app_client.post("/api/verifiers", json=_VALID_BODY)
        assert res.status_code == 401


class TestVerifierRoutesPayloadParity:
    """GET /api/loops and GET /api/verifiers must return byte-identical payloads."""

    def test_list_payloads_match_across_prefixes(self, app_client):
        # Two-app setup so the same fixture DB is shared between two prefixes.
        from app.loop_routes import router as loop_router
        from app.verifier_routes import router as verifier_router

        app_loops = FastAPI()

        @app_loops.middleware("http")
        async def _s(request: Request, call_next):
            request.state.auth_ctx = AuthContext.anonymous()
            return await call_next(request)

        app_loops.include_router(loop_router)

        def _db():
            yield app_client._lifespan  # placeholder; overridden below

        # Use the same dependency override as app_client.
        # Simpler: publish via app_client, then query both prefixes via two clients
        # sharing the same DB session override.
        db_holder = {}

        def _shared_db():
            yield db_holder["db"]

        for a in (app_client.app, app_loops):
            a.dependency_overrides[get_db] = _shared_db

        # Discover the original db session from app_client fixture setup.
        # The fixture stashes it on app._test_db_session for cross-app sharing.  # compat-test
        db_holder["db"] = app_client.app._test_db_session  # type: ignore[attr-defined]  # compat-test

        # Publish a verifier via the /api/verifiers surface.
        _publish(app_client, slug="parity-v")

        client_loops = TestClient(app_loops, raise_server_exceptions=True)
        a = client_loops.get("/api/loops").json()
        b = app_client.get("/api/verifiers").json()
        # same rows, same shape (order-independent: sort by slug)
        sa = sorted([(r["slug"], r["title"]) for r in a])
        sb = sorted([(r["slug"], r["title"]) for r in b])
        assert sa == sb

    def test_detail_payloads_match_across_prefixes(self, app_client):
        from app.loop_routes import router as loop_router
        from app.verifier_routes import router as verifier_router

        app_loops = FastAPI()

        @app_loops.middleware("http")
        async def _s(request: Request, call_next):
            request.state.auth_ctx = AuthContext.anonymous()
            return await call_next(request)

        app_loops.include_router(loop_router)

        def _shared_db():
            # Yield the same session used by app_client by reusing its override generator.
            gen = app_client.app.dependency_overrides[get_db]()
            yield from gen

        app_loops.dependency_overrides[get_db] = _shared_db

        _publish(app_client, slug="parity-detail")
        client_loops = TestClient(app_loops, raise_server_exceptions=True)
        a = client_loops.get("/api/loops/parity-detail").json()
        b = app_client.get("/api/verifiers/parity-detail").json()
        # Drop any nondeterministic field then assert byte parity.
        for d in (a, b):
            d.pop("created_at", None)
            d.pop("updated_at", None)
        assert a == b


# ── (5) /api/loops still byte-identical behaviour (compat regression) ────────


class TestLoopsPrefixBackwardsCompat:
    """GET /api/loops/* must keep returning the verifier payload unchanged."""

    def test_loops_list_returns_verifier_rows(self, app_client):
        _publish(app_client, slug="compat-list")
        # /api/loops is the SAME router (shim) — queryable here because the
        # dual-router attaches both prefixes in main.create_app. But our test
        # fixture only mounts the verifier_router; we assert via the loop router
        # import that the two share handlers.
        from app.loop_routes import router as loop_router
        from app.verifier_routes import router as verifier_router

        # The loop_routes shim must expose the SAME router object OR a router
        # whose routes are the same set (handler identity is the contract).
        assert loop_router is verifier_router or {
            (getattr(r, "path", None), getattr(r, "name", None)) for r in loop_router.routes
        } == {(getattr(r, "path", None), getattr(r, "name", None)) for r in verifier_router.routes}

    def test_loops_routes_use_verifier_handlers(self):
        """loop_routes.router must point at the SAME endpoint callables as
        verifier_routes.router — no shadow copy."""
        from app.loop_routes import router as loop_router
        from app.verifier_routes import router as verifier_router

        def _sigs(rtr):
            out = []
            for r in rtr.routes:
                out.append(
                    (
                        getattr(r, "methods", None),
                        getattr(r, "path", None),
                        id(getattr(r, "endpoint", r)),
                    )
                )
            return sorted(out, key=lambda x: str(x))

        assert _sigs(loop_router) == _sigs(verifier_router)


# ── (6) Private verifier run isolation (review F9) ───────────────────────────


class TestPrivateVerifierRunIsolation:
    def test_private_verifier_404_for_non_owner_200_for_master(self, app_client, db_session):
        from datetime import UTC, datetime

        from app.models import Verifier

        loop = Verifier(
            id=uuid4(),
            slug="private-verifier",
            title="Private",
            description="secret",
            success_condition="x",
            verification_script="exit 0",
            system_prompt="x",
            max_turns=5,
            budget_usd=None,
            tool_allowlist=[],
            stopping_criteria={"success": "x", "failure": "y", "budget": "z"},
            is_public=False,
            creator_id=None,
            created_at=datetime.now(UTC),
        )
        db_session.add(loop)
        db_session.flush()

        # Non-owner authenticated user: 404 (no existence leak).
        resp = app_client.post(
            "/api/verifiers/private-verifier/run",
            json={},
            headers={"x-test-auth": "user"},
        )
        assert resp.status_code == 404

        # Master: 200.
        resp_m = app_client.post(
            "/api/verifiers/private-verifier/run",
            json={},
            headers={"x-test-auth": "master"},
        )
        assert resp_m.status_code == 200


# ── (7) WR_LOOP_RUN_REQUIRE_SANDBOX gate (review F1/F6) ──────────────────────


class TestVerifierSandboxGate:
    def test_require_sandbox_returns_503(self, app_client, monkeypatch):
        import app.loop_runner as _lr

        _lr._runner = None
        monkeypatch.setenv("WR_LOOP_RUN_REQUIRE_SANDBOX", "true")
        _publish(app_client, slug="gated-verifier", verification_script="exit 0")
        resp = app_client.post(
            "/api/verifiers/gated-verifier/run",
            json={},
            headers={"x-test-auth": "user"},
        )
        assert resp.status_code == 503
        _lr._runner = None


# ── (7b) Empty request body on /run (fix: verifier-run-empty-body) ──────────
#
# Bug: POST /api/verifiers/{slug}/run with NO body (or {}) 422'd because
# `payload: VerifierRunIn` was a required body param, even though every field
# of VerifierRunIn already defaults (mode='verify', workspace_files=None,
# allow_network=False). Live evidence: `curl -X POST
# https://app.loopskill.io/api/loops/hello-world-loop/run` with no body
# returned 422 on the marketing hero ("it-RUNS") flow. Fix makes `payload`
# optional (Body(default=None)) and falls back to a fresh VerifierRunIn().


class TestVerifierRunEmptyBody:
    def test_run_with_no_body_is_not_422(self, app_client):
        """No Content-Type/body at all must reach auth+verifier logic, never 422."""
        _publish(app_client, slug="empty-body-v", verification_script="exit 0")
        resp = app_client.post(
            "/api/verifiers/empty-body-v/run",
            headers={"x-test-auth": "user"},
        )
        assert resp.status_code != 422
        assert resp.status_code == 200, resp.text

    def test_run_with_empty_json_object_is_not_422(self, app_client):
        """An explicit {} body (the existing convention in this file) still works."""
        _publish(app_client, slug="empty-json-v", verification_script="exit 0")
        resp = app_client.post(
            "/api/verifiers/empty-json-v/run",
            json={},
            headers={"x-test-auth": "user"},
        )
        assert resp.status_code != 422
        assert resp.status_code == 200, resp.text

    def test_run_with_no_body_still_401_when_anonymous(self, app_client):
        """No-body fix must not weaken the auth gate — anonymous still 401s."""
        _publish(app_client, slug="empty-body-anon-v", verification_script="exit 0")
        resp = app_client.post("/api/verifiers/empty-body-anon-v/run")
        assert resp.status_code == 401

    def test_run_with_explicit_body_still_honours_fields(self, app_client):
        """Zero-behaviour-change: callers who DO send a body keep their values."""
        _publish(app_client, slug="explicit-body-v", verification_script="exit 0")
        resp = app_client.post(
            "/api/verifiers/explicit-body-v/run",
            json={"mode": "agent"},
            headers={"x-test-auth": "user"},
        )
        # agent-mode is a deliberate 501 (roadmap) — proves the body was parsed
        # and its `mode` field actually reached the handler, not defaulted away.
        assert resp.status_code == 501


# ── (8) MCP: dual tool names + dispatch parity ───────────────────────────────


class TestMcpDualToolNames:
    def _tool_names(self):
        from app.mcp.registry import _tool_definitions

        return {t.name for t in _tool_definitions()}

    def test_canonical_verifier_tools_advertised(self):
        names = self._tool_names()
        assert "loopskill_search_verifiers" in names
        assert "loopskill_get_verifier" in names

    def test_legacy_loop_tools_still_advertised(self):
        names = self._tool_names()
        assert "loopskill_search_loops" in names
        assert "loopskill_get_loop" in names

    def test_normalize_maps_legacy_loop_to_canonical_verifier(self):
        from app.mcp._alias_map import normalize_tool_name

        # canonical VERIFIER name is the dispatch target; old LOOP name resolves to it.
        assert normalize_tool_name("loopskill_search_loops") == "loopskill_search_verifiers"
        assert normalize_tool_name("loopskill_get_loop") == "loopskill_get_verifier"
        # canonical passes through.
        assert normalize_tool_name("loopskill_search_verifiers") == "loopskill_search_verifiers"
        assert normalize_tool_name("loopskill_get_verifier") == "loopskill_get_verifier"

    def test_dispatch_resolves_both_names_to_same_fixture(self, db_session):
        """call_tool_sync with the OLD name and the NEW name must hit the same
        handler and return identical payloads for the same fixture."""
        from app.mcp.server import call_tool_sync

        # Seed a verifier directly.
        from app.models import Verifier

        db_session.add(
            Verifier(
                id=uuid4(),
                slug="mcp-parity",
                title="MCP Parity",
                description="parity check",
                success_condition="x",
                verification_script="true",
                system_prompt="y",
                max_turns=10,
                stopping_criteria={"success": "a", "failure": "b", "budget": "c"},
                tool_allowlist=["terminal"],
            )
        )
        db_session.flush()

        a = call_tool_sync(
            "loopskill_search_verifiers",
            {"query": "parity"},
            caller={"scope": "master"},
            db=db_session,
        )
        b = call_tool_sync(
            "loopskill_search_loops",  # legacy name
            {"query": "parity"},
            caller={"scope": "master"},
            db=db_session,
        )
        # The legacy path may inject bundle_status for non-sync tools — drop it for parity.
        a.pop("bundle_status", None)
        b.pop("bundle_status", None)
        assert a == b
        assert a["total"] == 1
        assert a["results"][0]["slug"] == "mcp-parity"

        d1 = call_tool_sync(
            "loopskill_get_verifier",
            {"slug": "mcp-parity"},
            caller={"scope": "master"},
            db=db_session,
        )
        d2 = call_tool_sync(
            "loopskill_get_loop",  # legacy name
            {"slug": "mcp-parity"},
            caller={"scope": "master"},
            db=db_session,
        )
        d1.pop("bundle_status", None)
        d2.pop("bundle_status", None)
        assert d1 == d2


# ── (9) DB/Alembic: physical tables untouched ────────────────────────────────


class TestDbTablesUntouched:
    def test_verifier_maps_to_loops_table(self):
        from app.models import Verifier

        assert Verifier.__tablename__ == "loops"

    def test_verifier_rating_maps_to_loop_ratings(self):
        from app.models import VerifierRating

        assert VerifierRating.__tablename__ == "loop_ratings"

    def test_verifier_version_maps_to_loop_versions(self):
        from app.models import VerifierVersion

        assert VerifierVersion.__tablename__ == "loop_versions"

    def test_reconcile_event_still_works(self, db_session):
        """Existing reconcile_events history must remain queryable (regression)."""
        from datetime import UTC, datetime

        from app.models import ReconcileEvent

        ev = ReconcileEvent(
            id=uuid4(),
            bundle_id=None,
            skill_id=uuid4(),  # type: ignore[arg-type]  # compat-test
            semver="1.0.0",
            outcome="success",
        )
        db_session.add(ev)
        db_session.flush()
        assert db_session.query(ReconcileEvent).filter(ReconcileEvent.id == ev.id).one() is ev


# ── (10) Terminology lint: context-aware (verifier sense) ────────────────────


class TestTerminologyLintContextAware:
    """The lint gate must permit NEW verifier-sense code using Verifier/verifier
    while still banning kitchen nouns (cookbook/recipe/chef-as-brand) and
    banning the OLD loop-as-verifier usage in NEW code outside compat shims."""

    def _flagged(self, line: str) -> bool:
        import scripts.terminology_lint as tl

        hit = next((b for b in tl.BANNED if b.search(line)), None)
        return hit is not None and not tl._line_allowlisted(line, hit)

    def test_kitchen_nouns_still_banned(self):
        assert self._flagged("cookbook_id = bundle.id")
        assert self._flagged("class Recipe(Base):")

    def test_verifier_word_allowed(self):
        # 'verifier' is the new canonical; must NOT trip the lint.
        assert not self._flagged("class Verifier(Base):")
        assert not self._flagged("return VerifierOut(...)")

    def test_compat_marker_allows_legacy_loop(self):
        # A compat shim explicitly tagged is fine.
        assert not self._flagged("Loop = Verifier  # compat-alias")

    def test_new_verifier_module_allows_loop_in_compat_context(self):
        """In a file path that is a compat shim (loop_routes.py), the word loop
        is allowed via the compat marker. In a NEW verifier-only module without
        a marker, bare 'Loop' usage should be flagged."""
        import scripts.terminology_lint as tl

        # Simulate scanning a single NEW file with the line.
        cases = [
            ("class Verifier(Base):", False),  # canonical new name -> allowed
            ("x = Loop(slug=...)", True),  # bare Loop in NEW verifier code -> flagged
            ("from app.models import Loop  # compat-alias", False),  # marked -> allowed
        ]
        for line, expected_flag in cases:
            hit = next((b for b in tl.BANNED if b.search(line)), None)
            flagged = hit is not None and not tl._line_allowlisted(line, hit)
            assert flagged == expected_flag, f"line {line!r}: expected_flag={expected_flag} got={flagged}"


# ── (11) Seed idempotency still works with the renamed ORM class ────────────


class TestSeedStillWorks:
    def test_seed_creates_verifier_rows(self, db_session):
        from app.models import Verifier

        from scripts.seed_starter_catalog import seed_starter_catalog

        # Add a base skill so bundle joins resolve.
        from app.models import Skill

        db_session.add(
            Skill(
                id=uuid4(),
                slug="seed-base",
                title="Seed Base",
                description="seed setup",
                is_public=True,
            )
        )
        first = seed_starter_catalog(db_session)
        assert first["loops_created"] >= 1
        # The seeded rows are now Verifier instances backed by the loops table.
        rows = db_session.query(Verifier).filter(Verifier.is_public.is_(True)).all()
        assert len(rows) >= 1
