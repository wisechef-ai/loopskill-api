"""Route tombstones — moneypath-C (T1 subtract, 2026-09-22).

A *tombstone* marks a public HTTP surface that the 30-day production
inventory (``docs/ops/used_30d-2026-09-22.json``) showed receiving ZERO
requests. The route is KEPT — registered, reachable, byte-identical
behaviour — but every response it produces carries ``X-Tombstoned: <since>``
and a hit counter ticks, so the *next* inventory can prove (rather than
guess) whether anything out there still depends on it. Nothing is deleted
here; deletion is a later, separate decision gated on the ledger
(``docs/ops/tombstones.md``) and its restore predicate.

Design — WHY a marker + ASGI middleware, not a signature-rewriting wrapper
--------------------------------------------------------------------------
FastAPI resolves an endpoint's dependencies from ``inspect.signature`` and
``endpoint.__globals__``. A ``functools.wraps`` closure changes ``__globals__``
(string annotations in the handler's module stop resolving) and injecting a
``Response`` parameter collides with handlers that already declare one. Both
failure modes are silent until request time. So :func:`tombstoned` does the
minimum: it stamps the *function object* (``__tombstoned__``), registers it,
and returns the SAME function — FastAPI sees exactly what it saw before.

The header is attached by :class:`TombstoneHeaderMiddleware`, a pure-ASGI
middleware that inspects ``scope["endpoint"]`` at ``http.response.start``.
Starlette's router mutates the shared ``scope`` dict with the matched
endpoint, so the middleware sees it regardless of whether the handler
returned normally, raised ``HTTPException`` from a dependency (401/403/404),
or was short-circuited by an exception handler. That means the header is
present on *every* response the route produced — including error paths —
which is what an honest "is anyone still calling this?" signal needs.

Counter: ``TOMBSTONE_HITS`` (handler qualname -> int). If ``app.metrics``
exists and exposes ``increment(name, **labels)`` it is also called; today no
such module exists, so the in-process dict is the only sink.
"""

from __future__ import annotations

import contextlib
import logging
from collections import Counter
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

HEADER_NAME = "X-Tombstoned"
HEADER_NAME_BYTES = HEADER_NAME.lower().encode("latin-1")

#: Registry of tombstoned endpoints: handler qualname -> metadata.
TOMBSTONES: dict[str, dict[str, str]] = {}

#: Hit counter (handler qualname -> requests served since process start).
TOMBSTONE_HITS: Counter[str] = Counter()

_ENABLED = True


def _qualname(fn: Callable[..., Any]) -> str:
    return f"{fn.__module__}.{getattr(fn, '__qualname__', getattr(fn, '__name__', '?'))}"


def tombstoned(*, since: str, ledger: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a route handler as tombstoned since ``since`` (ISO date).

    Returns the ORIGINAL function object (no wrapper — see module doc). The
    marker attributes are read by :class:`TombstoneHeaderMiddleware`.
    ``ledger`` is the path of the markdown ledger that carries the restore
    predicate; it is recorded so the marker is self-describing in a REPL.
    """

    def _decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        fn.__tombstoned__ = since  # type: ignore[attr-defined]
        fn.__tombstone_ledger__ = ledger  # type: ignore[attr-defined]
        TOMBSTONES[_qualname(fn)] = {"since": since, "ledger": ledger}
        return fn

    return _decorate


def is_tombstoned(fn: Any) -> str | None:
    """Return the ``since`` date if ``fn`` is a tombstoned endpoint, else None."""
    return getattr(fn, "__tombstoned__", None)


@contextlib.contextmanager
def disabled() -> Iterator[None]:
    """Temporarily switch the header/counter off (tests: prove status parity)."""
    global _ENABLED
    prev = _ENABLED
    _ENABLED = False
    try:
        yield
    finally:
        _ENABLED = prev


def _record_hit(fn: Any) -> None:
    name = _qualname(fn)
    TOMBSTONE_HITS[name] += 1
    try:  # optional metrics sink — absent today, kept cheap and non-fatal
        from app import metrics as _metrics  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - no metrics module is the normal case
        return
    inc = getattr(_metrics, "increment", None)
    if callable(inc):
        try:
            inc("tombstoned_route_hits", route=name)
        except Exception as exc:  # noqa: BLE001 - metrics must never break a request
            logger.debug("tombstone metrics sink failed: %s", exc)


def _resolve_endpoint(scope: dict) -> Any:
    """Find the handler Starlette WOULD dispatch to for ``scope`` (first match).

    Used only when an outer middleware answered before the router ran, so
    ``scope["endpoint"]`` was never set. Mirrors Starlette's routing: iterate
    the app's routes in registration order and take the first full match.
    """
    from starlette.routing import Match

    app = scope.get("app")
    router = getattr(app, "router", None)
    if router is None:
        return None
    for route in getattr(router, "routes", []):
        try:
            match, child = route.matches(scope)
        except Exception:  # noqa: BLE001 - never let header resolution break a response
            continue
        if match == Match.FULL:
            return child.get("endpoint") or getattr(route, "endpoint", None)
    return None


class TombstoneHeaderMiddleware:
    """Pure-ASGI middleware: add ``X-Tombstoned`` when the matched endpoint is marked."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message: dict) -> None:
            if message.get("type") == "http.response.start" and _ENABLED:
                endpoint = scope.get("endpoint")
                if endpoint is None:
                    # Short-circuited before the router ran (APIKeyMiddleware
                    # 401/403, rate-limit 429): resolve the route ourselves so
                    # the header still lands on auth-rejected responses.
                    endpoint = _resolve_endpoint(scope)
                since = is_tombstoned(endpoint)
                if since is not None:
                    headers = [
                        (k, v) for (k, v) in message.get("headers", []) if k.lower() != HEADER_NAME_BYTES
                    ]
                    headers.append((HEADER_NAME_BYTES, since.encode("latin-1")))
                    message = {**message, "headers": headers}
                    _record_hit(endpoint)
            await send(message)

        await self.app(scope, receive, _send)
