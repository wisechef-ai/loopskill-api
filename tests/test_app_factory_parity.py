"""Issue #357 — tests/_app_factory.py drift regression guard.

``tests/_app_factory.py`` (``build_test_app``) is a hand-maintained mirror of
``app.main.create_app()``'s router wiring, kept separate because
``create_app()``'s lifespan boots the Discord bot + MCP StreamableHTTP session
manager and needs full prod config. Every time a router is added to
``create_app`` without a matching entry in ``_ROUTER_SPECS``, factory-based
tests silently stop exercising that route (consumer-contract drift) —
see #357 for the concrete case: 6 routers were missing entirely and the
personalities router had its ``/api`` prefix double-applied (baked into the
router AND passed again to ``include_router``), so every accidental
factory-based test against ``/api/personalities/*`` was hitting a 404 through
a nonexistent ``/api/api/personalities/*`` path instead of the real route.

This test asserts route-set equality (method, path) between the two apps so
the drift can never silently reappear — it fails loudly instead.
"""

from __future__ import annotations

from app.main import create_app
from tests._app_factory import build_test_app

# Routes that legitimately exist ONLY on create_app() and have no reason to
# be mirrored by build_test_app():
#   - "/" — the root metadata endpoint, defined inline in create_app() after
#     all router mounts; cosmetic, not a feature surface under test.
#   - /api/mcp/sse, /api/mcp/messages/, /api/mcp/healthz — the MCP
#     SSE/legacy-HTTP transport routes. Requires the StreamableHTTP session
#     manager's lifespan (Discord bot + MCP server boot), which is exactly
#     why build_test_app() exists as a separate, lifespan-free factory in
#     the first place (see module docstring). MCP-transport behavior is
#     covered by tests that call create_app() directly (e.g. test_mcp_*).
_CREATE_APP_ONLY = {
    ("GET", "/"),
    ("GET", "/api/mcp/sse"),
    ("POST", "/api/mcp/messages/"),
    ("GET", "/api/mcp/healthz"),
}


def _route_set(app) -> set[tuple[str, str]]:
    """(method, path) pairs for every concrete HTTP route on the app.

    Skips the StreamableHTTP ASGI sub-app mount (not an APIRoute — it has no
    discrete method/path pairs to compare) and the bare mount routes
    (``app.mount`` items), matching only real ``APIRoute`` instances.
    """
    pairs: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        path = getattr(route, "path", None)
        if not methods or not path:
            continue
        for method in methods:
            if method == "HEAD":  # FastAPI auto-adds HEAD for every GET; not interesting
                continue
            pairs.add((method, path))
    return pairs


def test_factory_route_set_matches_create_app(db_session, monkeypatch):
    """Every route create_app() mounts must also exist on build_test_app().

    Intentionally NOT bidirectional: create_app() also owns the MCP
    StreamableHTTP mount and the Discord-bot-adjacent lifespan wiring that
    build_test_app() has no reason to replicate. The contract that matters is
    one-directional — a factory-based test must never be silently testing a
    route that doesn't exist in prod, and must never be silently skipped
    because prod has a route the factory lacks.
    """
    prod_app = create_app()
    test_app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)

    prod_routes = _route_set(prod_app)
    test_routes = _route_set(test_app)

    missing_from_factory = (prod_routes - test_routes) - _CREATE_APP_ONLY
    assert not missing_from_factory, (
        "Routes mounted in app.main.create_app() but missing from "
        "tests._app_factory.build_test_app() — add the missing router(s) to "
        f"_ROUTER_SPECS: {sorted(missing_from_factory)}"
    )


def test_personality_routes_not_double_prefixed(db_session, monkeypatch):
    """Regression for #357: /api/api/personalities must never exist.

    personality_routes.router already bakes in prefix="/api/personalities".
    The old factory spec re-applied prefix="/api" on top of that, doubling
    the api segment. Checked against the route table directly (not a live
    HTTP call) because ``APIKeyMiddleware`` returns 401 for ANY unauthed
    request before routing ever gets a chance to 404 a nonexistent path —
    an HTTP-level probe can't distinguish "route missing" from "route
    exists but unauthenticated".
    """
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    paths = {getattr(route, "path", None) for route in app.routes}

    assert "/api/api/personalities" not in paths, (
        "doubled /api/api/personalities path must not exist as a route "
        "(personality_routes.router already bakes in the /api prefix)"
    )
    assert "/api/personalities" in paths, (
        "/api/personalities must be mounted at the correct single-prefixed path"
    )
