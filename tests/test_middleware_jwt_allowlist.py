"""Regression: /api/me/* paths must be in the JWT-auth allowlist, not API-key.

Bug history
-----------
After WIS-660 mounted /api/me/referral-code and /api/me/referrals, the
APIKeyMiddleware allowlist (`JWT_AUTH_PREFIXES`) did not include
``/api/me/`` — so every request to those routes hit the API-key gate
first and returned ``{"detail": "Invalid or missing x-api-key header"}``
even when the user had a valid JWT cookie.

Effect: the /library "Refer & Earn" card stayed on "Loading…" forever
because ``loadReferrals()`` got a 401, and the referral money loop
never closed for any actual logged-in user.

Contract pinned:
1. /api/me/referral-code and /api/me/referrals must NOT 401 with
   "Invalid or missing x-api-key header" — they must fall through to
   the route's JWT check (which is allowed to return its own 401
   "login_required" if no cookie is present).
2. /api/subscriptions/downgrade likewise — added preventatively so the
   next /api/me/-style addition doesn't reintroduce the same bug.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware import APIKeyMiddleware


def _make_test_app() -> FastAPI:
    """FastAPI app that mounts the middleware + a stub /api/me/x route.

    We don't include the real referral_router here because that requires
    a fully-stubbed DB session and the User model. The contract under
    test is the *middleware*: does it allow /api/me/* to bypass the
    x-api-key gate? The route's own auth check is exercised by
    test_referrals.py.
    """
    app = FastAPI()
    app.add_middleware(APIKeyMiddleware)

    @app.get("/api/me/probe")
    async def _me_probe():
        # If the middleware lets us through, this responds 200.
        # If the middleware blocks us, this never runs and the response
        # is the middleware's 401.
        return {"ok": True}

    @app.get("/api/some-random-admin-endpoint")
    async def _admin():
        # A control endpoint that SHOULD be gated by API key — proves
        # the middleware is actually running.
        return {"ok": True}

    return app


class TestApiMeAllowlist:
    def test_api_me_path_bypasses_api_key_gate(self):
        client = TestClient(_make_test_app())
        # No x-api-key header, no JWT cookie. The middleware must let
        # this request through to the route handler so route-level auth
        # can decide. (The route here has no auth at all, so 200.)
        r = client.get("/api/me/probe")
        assert r.status_code == 200, (
            f"/api/me/* must be in JWT_AUTH_PREFIXES so the API-key gate "
            f"doesn't intercept JWT-cookie auth. Got {r.status_code} "
            f"with body {r.text!r}"
        )
        assert r.json() == {"ok": True}

    def test_api_me_path_does_not_get_invalid_api_key_error(self):
        client = TestClient(_make_test_app())
        r = client.get("/api/me/probe")
        # The specific error message is what /library was seeing.
        assert "x-api-key" not in r.text.lower(), (
            f"/api/me/* must not surface the API-key error. Got {r.text!r}"
        )

    def test_control_endpoint_still_blocks_without_api_key(self):
        """Sanity: prove the middleware is running and would block other paths."""
        client = TestClient(_make_test_app())
        r = client.get("/api/some-random-admin-endpoint")
        assert r.status_code == 401
        assert "x-api-key" in r.text.lower()

    def test_subscriptions_downgrade_bypasses_api_key_gate(self):
        """Belt-and-suspenders: /api/subscriptions/* added preventatively."""
        client = TestClient(_make_test_app())
        # Mount a probe under /api/subscriptions/ to exercise that prefix.
        app = _make_test_app()

        @app.get("/api/subscriptions/probe")
        async def _probe():
            return {"ok": True}

        client = TestClient(app)
        r = client.get("/api/subscriptions/probe")
        assert r.status_code == 200, (
            f"/api/subscriptions/* must be in JWT_AUTH_PREFIXES. "
            f"Got {r.status_code} with body {r.text!r}"
        )


class TestCanonicalPrefixParity:
    """A compat-alias must never be reachable while its CANONICAL name is not.

    Bug history (mesh_0408 W5, found independently by two workers)
    -------------------------------------------------------------
    The Phase 3+4 rename made ``/api/bundle-deploy`` canonical and kept
    ``/api/cookbook-deploy`` as a compat-alias — both mounted from the same
    handler (``bundle_deployment_routes.py:436-437``). ``JWT_AUTH_PREFIXES``
    was only ever updated for the OLD name.

    Effect: the canonical prefix falls through to the api-key gate while its
    routes depend on ``get_current_user_optional`` (JWT only). A portal request
    is rejected by middleware before the route runs; an api-keyed request
    reaches the route and gets ``401 login_required``. So the canonical
    vocabulary was aspirational — only the alias actually worked.

    Every other test in the suite builds the app WITHOUT middleware, which is
    precisely why nothing caught it.
    """

    def _probe_app(self, path: str) -> TestClient:
        app = _make_test_app()

        @app.get(path)
        async def _probe():
            return {"ok": True}

        return TestClient(app)

    def test_canonical_bundle_deploy_prefix_bypasses_api_key_gate(self):
        r = self._probe_app("/api/bundle-deploy/probe").get("/api/bundle-deploy/probe")
        assert r.status_code == 200, (
            "/api/bundle-deploy/* is the CANONICAL prefix and must be in "
            f"JWT_AUTH_PREFIXES. Got {r.status_code} with body {r.text!r}"
        )

    def test_compat_alias_still_works(self):
        """CONTROL. If this fails the probe is void, not passing."""
        r = self._probe_app("/api/cookbook-deploy/probe").get("/api/cookbook-deploy/probe")
        assert r.status_code == 200, (
            f"CONTROL FAILED — the compat-alias is not JWT-routed either, so "
            f"the parity assertion above proves nothing. Got {r.status_code}."
        )

    def test_every_aliased_deploy_prefix_agrees(self):
        """Neither name may be gated differently from the other.

        Asserted as a pair rather than individually: the defect is a
        DISAGREEMENT between two mounts of one handler, and a test that checks
        only one name cannot see it.
        """
        gated = {
            name: any(
                f"/api/{name}/probe".startswith(p) for p in APIKeyMiddleware.JWT_AUTH_PREFIXES
            )
            for name in ("bundle-deploy", "cookbook-deploy")
        }
        assert len(set(gated.values())) == 1, (
            "A canonical prefix and its compat-alias mount the SAME handler and "
            f"must share auth routing. Divergence: {gated}"
        )
