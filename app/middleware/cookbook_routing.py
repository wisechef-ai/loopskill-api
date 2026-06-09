"""CookbookHostMiddleware — white-label custom-domain routing for Pro+ cookbooks.

spotify_0608 Ph A — re-homed from the retired ``BucketHostMiddleware``. Cookbook
is the survivor primitive (D1); white-label "host your cookbook on your own
domain" survives as a Pro+ capability keyed on ``cookbooks.custom_domain``.
"""

from __future__ import annotations

import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("wiserecipes.middleware")


class CookbookHostMiddleware(BaseHTTPMiddleware):
    """White-label custom-domain routing for Pro+ cookbooks (spotify_0608 Ph A).

    When a request arrives whose Host header matches a ``cookbooks.custom_domain``
    row, the middleware stamps ``request.state.cookbook_id`` and ``cookbook_slug``
    so downstream catalog handlers can scope responses to that cookbook. The
    middleware is non-mutating for any non-cookbook host and never alters the
    response body — scoping is opt-in by handlers reading ``request.state``.
    """

    # Hosts that are NEVER treated as a custom domain regardless of DB state.
    SKIP_HOSTS = {"localhost", "127.0.0.1", "testserver"}

    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower().strip()
        if not host or host in self.SKIP_HOSTS:
            return await call_next(request)

        from app.database import SessionLocal
        from app.models import Cookbook

        db = SessionLocal()
        try:
            cookbook = db.query(Cookbook).filter(Cookbook.custom_domain == host).first()
            if cookbook:
                request.state.cookbook_id = str(cookbook.id)
                request.state.cookbook_slug = cookbook.slug
                request.state.cookbook_theme = cookbook.theme_json
        # Rationale: cookbook domain lookup failure must not break the request; log and continue
        except Exception as e:  # noqa: BLE001
            logger.warning("CookbookHostMiddleware lookup failed for host=%s: %s", host, e)
        finally:
            db.close()
        return await call_next(request)
