"""moneypath-C — tombstone contract test (parametrized over docs/ops/tombstones.md).

For EVERY route row in the ledger:

1. the route is still registered (kept, not deleted) — resolving the path
   against the production route table finds a handler marked ``__tombstoned__``;
2. an anonymous request returns the SAME status code with the tombstone
   machinery on as with it off (behaviour unchanged);
3. with it on, the response carries ``X-Tombstoned: <since>``.

Plus: every ``@tombstoned`` handler in the app is listed in the ledger (no
silent tombstones), and no protected surface is tombstoned.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import tombstone

LEDGER = Path(__file__).resolve().parents[1] / "docs" / "ops" / "tombstones.md"
_ROW = re.compile(r"^\|\s*`(?P<path>[^`]+)`\s*\|\s*(?P<method>[A-Z|]+)\s*\|\s*(?P<used>\d+)\s*\|\s*(?P<since>\d{4}-\d{2}-\d{2})\s*\|")

PROTECTED_PREFIXES = (
    "/api/healthz",
    "/api/health",
    "/skill",
    "/SKILL.md",
    "/fleet/skill",
    "/fleet/SKILL.md",
    "/api/stats",
    "/.well-known",
    "/api/mcp",
    "/api/auth",
    "/api/stripe",
    "/api/checkout",
    "/api/billing",
    "/api/subscriptions",
    "/api/admin",
)


def _ledger_rows() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if m:
            rows.append((m["method"], m["path"], m["since"]))
    assert rows, f"no ledger rows parsed from {LEDGER}"
    return rows


ROWS = _ledger_rows()


def _fill(path: str) -> str:
    """Substitute a syntactically valid placeholder for every path parameter."""
    def _sub(m: re.Match[str]) -> str:
        name = m.group(1).split(":")[0]
        if name.endswith("_id") and name not in ("request_id",):
            return str(uuid4())
        return "moneypath-c-probe"

    return re.sub(r"\{([^}]+)\}", _sub, path)


@pytest.fixture(scope="module")
def _app(db_session_module, module_monkeypatch):
    """The REAL production app (``app.main.create_app``), not the test factory.

    Why: ``tests/_app_factory.py`` is a hand-maintained mirror and has drifted
    (it lacks bundle_converge / sse / federation_filter / loop_pack / mesh /
    wisechef routers and mounts personalities under a doubled prefix). A
    "route kept" contract must be checked against the route table that
    actually ships. Lifespan (Discord bot, MCP session manager) is not entered
    because TestClient is used without ``with``; the DB session is repointed
    exactly as the factory does; the per-IP rate limiter is widened so the
    ~2x181 probe requests from one client cannot 429 and skew status parity.
    """
    from app.database import get_db
    from app.main import create_app
    from app.middleware import RateLimitMiddleware
    from tests._app_factory import _SharedSessionFactory

    module_monkeypatch.setattr("app.database.SessionLocal", _SharedSessionFactory(db_session_module))
    app = create_app()
    for m in app.user_middleware:
        if m.cls is RateLimitMiddleware:
            m.kwargs["max_requests"] = 10**9

    def _override_get_db():
        yield db_session_module

    app.dependency_overrides[get_db] = _override_get_db
    return app


@pytest.fixture(scope="module")
def module_monkeypatch():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def db_session_module(engine_fixture):
    from sqlalchemy.orm import sessionmaker

    conn = engine_fixture.connect()
    tx = conn.begin()
    session = sessionmaker(bind=conn)()
    yield session
    session.close()
    tx.rollback()
    conn.close()


@pytest.fixture(scope="module")
def route_table(_app) -> list[APIRoute]:
    return [r for r in _app.routes if isinstance(r, APIRoute)]


def _match(route_table: list[APIRoute], method: str, path: str) -> APIRoute:
    for r in route_table:
        if r.path == path and method in r.methods:
            return r
    raise AssertionError(f"{method} {path} is no longer registered — tombstoned routes must be KEPT")


@pytest.mark.parametrize("method,path,since", ROWS, ids=[f"{m} {p}" for m, p, _ in ROWS])
def test_tombstoned_route_kept_same_status_and_headed(_app, route_table, method, path, since):
    route = _match(route_table, method, path)
    assert tombstone.is_tombstoned(route.endpoint) == since

    url = _fill(path)
    body = {} if method in ("POST", "PATCH", "PUT") else None
    # no `with`: lifespan stays off; follow_redirects=False so a 302 route's
    # own response is asserted, not whatever its Location resolves to.
    c = TestClient(_app, raise_server_exceptions=False, follow_redirects=False)
    with tombstone.disabled():
        baseline = c.request(method, url, json=body)
    assert tombstone.HEADER_NAME not in baseline.headers
    live = c.request(method, url, json=body)

    assert live.status_code == baseline.status_code, (
        f"{method} {path}: status changed {baseline.status_code} -> {live.status_code}"
    )
    assert live.headers.get(tombstone.HEADER_NAME) == since


def test_every_tombstoned_handler_is_in_ledger(route_table):
    ledger = {(m, p) for m, p, _ in ROWS}
    marked = {
        (meth, r.path)
        for r in route_table
        if tombstone.is_tombstoned(r.endpoint)
        for meth in r.methods
    }
    assert marked == ledger, {
        "marked_not_in_ledger": sorted(marked - ledger),
        "ledger_not_marked": sorted(ledger - marked),
    }


@pytest.mark.parametrize("method,path,since", ROWS, ids=[f"{m} {p}" for m, p, _ in ROWS])
def test_no_protected_surface_tombstoned(method, path, since):
    assert path != "/"
    for p in PROTECTED_PREFIXES:
        assert not (path == p or path.startswith(p + "/") or (p in ("/skill", "/api/health", "/api/stats") and path.startswith(p))), (
            f"{path} is a protected surface and must not be tombstoned"
        )
    assert "/install" not in path, f"{path} is an install surface and must not be tombstoned"


def test_hit_counter_increments(_app, route_table):
    method, path, _ = ROWS[0]
    route = _match(route_table, method, path)
    key = f"{route.endpoint.__module__}.{route.endpoint.__qualname__}"
    before = tombstone.TOMBSTONE_HITS[key]
    TestClient(_app, raise_server_exceptions=False, follow_redirects=False).request(
        method, _fill(path), json={} if method != "GET" else None
    )
    assert tombstone.TOMBSTONE_HITS[key] == before + 1
