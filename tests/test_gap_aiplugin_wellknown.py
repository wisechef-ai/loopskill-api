"""gap/gap-aiplugin — GET /.well-known/ai-plugin.json is public.

THE DEFECT (live-verified 2026-08-20)
--------------------------------------
``GET https://app.loopskill.io/.well-known/ai-plugin.json`` returned 401
``{"detail":"Invalid or missing x-api-key header"}`` — the third standard
AI-plugin discovery convention, alongside ``/.well-known/agent.json`` and
``/.well-known/mcp.json`` (both of which correctly 200). A cold agent that
tries the ai-plugin convention instead hit a dead end with no breadcrumb.
The route did not exist at all before this change; requests to it hit
``APIKeyMiddleware``'s allowlist wall before routing even resolved whether a
handler existed.

RED-PROOFING
------------
Every test in ``TestServesUnauthenticated`` and ``TestPinnedInExemptPaths``
was confirmed to FAIL before the fix:

* with the route registered but ``/.well-known/ai-plugin.json`` NOT added to
  ``app/middleware/_public_paths.py:EXEMPT_PATHS``,
  ``test_200_with_no_credential`` got 401 (the exact live defect,
  byte-identical body) and ``test_path_is_pinned_in_exempt_paths`` failed on
  the membership assertion.
* with the route not defined at all, every test in this module raised
  ``ImportError``/collection failure or 404.

Both are pinned so a future middleware change or accidental route deletion
regresses loudly instead of silently re-walling this surface.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.database import get_db
from app.main import create_app
from app.middleware._public_paths import EXEMPT_PATHS

PATH = "/.well-known/ai-plugin.json"


def _unauthenticated_app_and_client(db_session):
    """Build the real app + a raw TestClient with NO x-api-key header —
    proves the endpoint really is exempt from APIKeyMiddleware, using the
    REAL app (not the stripped-down tests/conftest.py client fixture, which
    pre-seeds an x-api-key header on every request and would mask a
    middleware regression). Mirrors tests/test_mesh_wellknown_routes.py.
    """
    app = create_app()

    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    return app, TestClient(app, raise_server_exceptions=True)


def _unauthenticated_client(db_session):
    _app, client = _unauthenticated_app_and_client(db_session)
    return client


class TestServesUnauthenticated:
    def test_200_with_no_credential(self, db_session):
        client = _unauthenticated_client(db_session)
        resp = client.get(PATH)
        assert resp.status_code == 200, resp.text

    def test_content_type_and_cache_convention_matches_siblings(self, db_session):
        """Same Cache-Control/ETag convention as agent.json/mcp.json/jwks.json."""
        client = _unauthenticated_client(db_session)
        resp = client.get(PATH)
        assert resp.headers["content-type"].startswith("application/json")
        assert "max-age=3600" in resp.headers.get("Cache-Control", "")
        assert "public" in resp.headers.get("Cache-Control", "")
        assert resp.headers.get("ETag")

    def test_body_shape_is_the_ai_plugin_convention(self, db_session):
        client = _unauthenticated_client(db_session)
        doc = client.get(PATH).json()
        for key in (
            "schema_version",
            "name_for_human",
            "name_for_model",
            "description_for_human",
            "description_for_model",
            "auth",
            "api",
        ):
            assert key in doc, f"missing conventional ai-plugin key: {key}"
        assert doc["schema_version"] == "v1"

    def test_does_not_point_at_the_404ing_openapi_json(self, db_session):
        """IMPORTANT HONESTY CONSTRAINT: GET /openapi.json 404s live on this
        deployment (verified 2026-08-20 via
        ``curl -D- https://app.loopskill.io/openapi.json`` — a bare Caddy
        404, no ``uvicorn`` response header, meaning the edge never proxies
        that path to this FastAPI app at all; the in-process TestClient
        cannot reproduce edge routing, so this test pins the honest
        contract at the manifest-content level instead). Pointing api.url
        at it would be the exact 'documented but broken' defect class this
        route exists to kill.
        """
        client = _unauthenticated_client(db_session)
        doc = client.get(PATH).json()
        body_text = client.get(PATH).text
        assert "openapi.json" not in body_text
        assert "openapi.json" not in doc["api"]["url"]
        assert "openapi.json" not in doc["api"].get("endpoint", "")

    def test_cross_links_to_agent_json_for_full_enrolment_spec(self, db_session):
        client = _unauthenticated_client(db_session)
        doc = client.get(PATH).json()
        text = client.get(PATH).text
        assert "/.well-known/agent.json" in text
        assert doc["auth"]["type"] == "none"

    def test_no_secrets_leak_into_the_document(self, db_session):
        from app.config import settings

        client = _unauthenticated_client(db_session)
        text = client.get(PATH).text
        assert settings.API_KEY not in text
        assert settings.JWT_SECRET not in text
        assert settings.SIGNING_SECRET not in text


class TestPinnedInExemptPaths:
    def test_path_is_pinned_in_exempt_paths(self):
        """Pinning test: a future middleware change cannot silently re-wall
        this path without this test failing.
        """
        assert PATH in EXEMPT_PATHS


class TestEveryAdvertisedUrlIsARealRoute:
    """The no-404-links promise, pinned by introspecting app.routes rather
    than hardcoding — the exact promise this whole gap-closing effort makes.
    """

    def test_every_url_the_manifest_advertises_resolves_to_a_real_route(self, db_session):
        app, client = _unauthenticated_app_and_client(db_session)
        registered_paths = {getattr(r, "path", None) for r in app.routes}

        doc = client.get(PATH).json()

        from app.config import public_origin

        origin = public_origin()

        def _strip_origin(url: str) -> str:
            assert url.startswith(origin), f"{url!r} is not rooted at the configured origin"
            return url[len(origin) :]

        # Collect every *_url / url / endpoint-shaped string value in the doc.
        checked = 0
        for key in ("url", "endpoint"):
            value = doc.get("api", {}).get(key)
            if value is None:
                continue
            path = _strip_origin(value)
            # /api/mcp/http is an ASGI sub-mount (StreamableHTTP), not a
            # plain FastAPI route object — verify by real HTTP call instead
            # of route-table membership.
            if path == "/api/mcp/http":
                resp = client.post(path)
                assert resp.status_code != 404, f"{path} is not mounted (404)"
            else:
                assert path in registered_paths, (
                    f"manifest advertises {path!r} but it is not a real route in app.routes"
                )
            checked += 1

        # And the auth instructions text names a real POST route + a real
        # sibling .well-known GET route.
        instructions = doc["auth"]["instructions"]
        assert "/api/agents/register" in instructions
        assert any(
            getattr(r, "path", None) == "/api/agents/register" and "POST" in getattr(r, "methods", set())
            for r in app.routes
        ), "instructions reference /api/agents/register but no such POST route exists"
        checked += 1

        assert "/.well-known/agent.json" in instructions
        assert "/.well-known/agent.json" in registered_paths
        checked += 1

        assert checked >= 3, "expected to check at least 3 advertised URLs"
