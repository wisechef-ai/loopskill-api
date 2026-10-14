"""Route-set parity gate — tests/_app_factory.py vs app.main.create_app.

Issue #357: tests/_app_factory.py had drifted from create_app() — 6 routers
were missing entirely (mesh, sse, bundle_converge, federation_filter,
loop_pack, wisechef/marketing) and personality_routes was double-prefixed
(/api/api/personalities/*), so tests exercising the factory could pass while
production routing genuinely differed underneath them (a consumer-contract
blind spot: moneypath-C worked around it by calling create_app() directly
instead of fixing the factory).

This test is the structural fix the issue asked for: assert route-set
equality between the two builders so a FUTURE new router added to create_app
(and never mirrored into _app_factory.py) fails loudly here instead of
silently degrading every factory-based test that touches it.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.database import get_db
from app.main import create_app
from tests._app_factory import build_test_app


def _route_keys(app) -> set[tuple[str, str]]:
    """(path, method) pairs for every concrete APIRoute (skip mounts/websockets)."""
    keys = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            if method == "HEAD":
                # FastAPI auto-adds HEAD for every GET; not an independently
                # declared route, so comparing it adds no signal.
                continue
            keys.add((route.path, method))
    return keys


@pytest.fixture(scope="module")
def _create_app_routes() -> set[tuple[str, str]]:
    # create_app() only wires routes/middleware at construction time; its
    # lifespan (Discord bot, MCP session manager) never runs unless the
    # returned app is used as an ASGI app under a real event loop (e.g. via
    # TestClient's context-manager form), which this test deliberately
    # avoids — mirroring test_mesh_routes.py's existing bare create_app()
    # usage elsewhere in this suite.
    app = create_app()
    return _route_keys(app)


@pytest.fixture
def _factory_routes(db_session, monkeypatch) -> set[tuple[str, str]]:
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return _route_keys(app)


class TestAppFactoryRouteParity:
    def test_factory_has_no_routes_missing_from_create_app(self, _factory_routes, _create_app_routes):
        """Every route the test factory mounts must genuinely exist in prod.

        (Guards against the OPPOSITE drift direction: a stale/renamed router
        left in the factory after create_app moved on.)
        """
        extra = _factory_routes - _create_app_routes
        assert not extra, (
            "tests/_app_factory.py mounts routes that do not exist in "
            f"app.main.create_app() — stale/renamed router entry: {sorted(extra)[:20]}"
        )

    def test_create_app_has_no_routes_missing_from_factory(self, _factory_routes, _create_app_routes):
        """RED-proofs issue #357: every create_app() route must be reachable
        through the shared test factory, or factory-based tests are silently
        exercising a different route table than production."""
        missing = _create_app_routes - _factory_routes
        assert not missing, (
            "app.main.create_app() has routes tests/_app_factory.py never "
            f"mounts — factory-based tests have a routing blind spot: {sorted(missing)[:20]}"
        )

    def test_personalities_prefix_is_not_doubled(self, _factory_routes):
        """Pins the second half of issue #357: no /api/api/... route exists."""
        doubled = [path for path, _method in _factory_routes if path.startswith("/api/api/")]
        assert not doubled, f"double-prefixed route(s) found: {doubled}"
