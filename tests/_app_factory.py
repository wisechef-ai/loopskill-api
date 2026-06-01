"""Shared test-app builder — single source of truth for in-test FastAPI apps.

Before this module, every test file that needed an HTTP client hand-rolled its
own ``FastAPI()`` + router includes. Each one was missing something different:

* paywall / v6 tests forgot ``APIKeyMiddleware`` → ``request.state.auth_ctx``
  was never populated → every authed caller resolved as unpaid.
* counter / access tests forgot the ``access_router`` → ``/api/skills/access``
  fell through to ``/api/skills/{slug}`` and 404'd as "skill 'access' not
  found".

The production app is built by ``app.main.create_app``. We cannot call it
directly in unit tests (its lifespan boots the Discord bot + the MCP
StreamableHTTP session manager, and it needs full prod config), so this module
mirrors its middleware + router wiring exactly. When a router is added to
``create_app``, add it here too — that keeps the test surface honest.

Usage::

    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db, monkeypatch=mp)
    client = TestClient(app, headers={"x-api-key": some_key})

``APIKeyMiddleware`` opens its own DB session at request time via
``app.database.SessionLocal`` (NOT the ``get_db`` dependency). build_test_app
repoints ``SessionLocal`` at the very same ``db_session`` the route handlers
use — wrapped so ``.close()`` is a no-op — so the middleware sees exactly the
rows the test created, including uncommitted ones. That sidesteps the
SQLite-StaticPool single-connection conflict you get if the middleware opens
an independent session against the same in-memory engine.

``with_middleware=False`` skips ``APIKeyMiddleware`` for the rare test that
deliberately wants the raw, unauthenticated route behaviour.
"""
from __future__ import annotations

import importlib
from typing import Any

from fastapi import FastAPI

from app.database import get_db
from app.middleware import APIKeyMiddleware

# Router import specs mirroring app.main.create_app (lines 115-148).
# Each entry: (module_path, attr, prefix). prefix="" means mount at root.
# Kept as data so a missing optional dependency on a CI host degrades to a
# skipped router instead of an ImportError that kills the whole app.
_ROUTER_SPECS: list[tuple[str, str, str]] = [
    ("app.routes", "router", ""),
    ("app.routes", "utm_router", ""),
    ("app.skill_serve_routes", "skill_serve_router", ""),
    ("app.health_routes", "router", "/api"),
    ("app.access_routes", "router", "/api"),
    ("app.recipe_routes", "router", "/api"),
    ("app.install_routes", "router", "/api"),
    ("app.skill_routes", "router", "/api"),
    ("app.skill_files_routes", "router", "/api"),
    ("app.admin_routes", "router", ""),
    ("app.auth_routes", "router", ""),
    ("app.carousel.routes", "router", "/api"),
    ("app.sandbox.routes", "router", ""),
    ("app.creator_routes", "router", ""),
    ("app.publisher_routes", "router", ""),
    ("app.checkout_routes", "router", ""),
    ("app.api_key_routes", "router", ""),
    ("app.feedback_routes", "router", ""),
    ("app.canary", "router", ""),
    ("app.forks_routes", "router", ""),
    ("app.cookbook_routes", "router", ""),
    ("app.graph_routes", "router", ""),
    ("app.buckets_routes", "router", ""),
    ("app.heartbeat_routes", "router", ""),
    ("app.intent_survey_routes", "router", ""),
    ("app.skill_error_routes", "router", ""),
    ("app.transparency_routes", "router", ""),
    ("app.feedback_v1_routes", "router", ""),
    ("app.skill_patch_routes", "router", ""),
    ("app.recall_routes", "router", ""),
    ("app.recipify_routes", "router", ""),
    ("app.referral_routes", "router", ""),
    ("app.marketing_routes", "router", ""),
    ("app.share_token_routes", "router", ""),
]


def _mount_all_routers(app: FastAPI) -> None:
    """Mount every feature router create_app mounts.

    An optional router whose module fails to import (missing extra dependency
    on a bare CI host) is skipped rather than aborting app construction — the
    same tolerance the legacy per-file fixtures had via try/except.
    """
    for module_path, attr, prefix in _ROUTER_SPECS:
        try:
            module = importlib.import_module(module_path)
            router = getattr(module, attr)
        except Exception:  # noqa: BLE001
            # Rationale: an optional router with an unmet extra dependency must
            # not break the whole test app; the tests that need it will fail
            # loudly on their own assertions instead.
            continue
        if prefix:
            app.include_router(router, prefix=prefix)
        else:
            app.include_router(router)


class _SharedSessionFactory:
    """A ``SessionLocal`` stand-in that always hands back the test session.

    ``APIKeyMiddleware`` does ``db = SessionLocal(); ...; db.close()``. We hand
    back the test's own session every time, with ``.close()`` neutralised so
    the middleware cannot tear down the session the test fixture still owns.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    def __call__(self) -> Any:
        session = self._session

        class _NoCloseProxy:
            def __getattr__(self, name: str) -> Any:
                return getattr(session, name)

            def close(self) -> None:  # noqa: D401 - middleware lifecycle no-op
                """No-op: the test fixture owns this session's lifecycle."""

        return _NoCloseProxy()


def build_test_app(
    *,
    db_session: Any,
    monkeypatch: Any = None,
    with_middleware: bool = True,
) -> FastAPI:
    """Build a FastAPI app wired like production for use under TestClient.

    Args:
        db_session: the SQLAlchemy Session both the route handlers (via the
            ``get_db`` dependency) and ``APIKeyMiddleware`` (via the patched
            ``SessionLocal``) will use.
        monkeypatch: pytest ``monkeypatch`` fixture. Required when
            ``with_middleware`` is True — used to repoint
            ``app.database.SessionLocal`` at the shared test session.
        with_middleware: mount ``APIKeyMiddleware`` (default True). Set False
            only for a test that deliberately exercises raw, unauthenticated
            route behaviour.

    Returns:
        A configured FastAPI app. The caller wraps it in ``TestClient``.
    """
    app = FastAPI()

    if with_middleware:
        if monkeypatch is None:
            raise ValueError(
                "build_test_app(with_middleware=True) requires `monkeypatch` — "
                "APIKeyMiddleware opens its own SessionLocal() session and must "
                "be repointed at the shared test session."
            )
        monkeypatch.setattr(
            "app.database.SessionLocal", _SharedSessionFactory(db_session)
        )
        app.add_middleware(APIKeyMiddleware)

    _mount_all_routers(app)

    def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return app
