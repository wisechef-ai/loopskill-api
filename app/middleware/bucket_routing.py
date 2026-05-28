"""BucketHostMiddleware — white-label custom-domain routing for Pro+ buckets (Phase E.4)."""

from __future__ import annotations

import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("wiserecipes.middleware")


class BucketHostMiddleware(BaseHTTPMiddleware):
    """White-label custom-domain routing for Pro+ buckets (Phase E.4).

    When a request arrives whose Host header matches a `buckets.custom_domain`
    row, the middleware stamps `request.state.bucket_id` and `bucket_slug`
    so downstream catalog handlers can scope responses to that bucket. The
    middleware is non-mutating for any non-bucket host and never alters the
    response body — scoping is opt-in by handlers reading `request.state`.
    """

    # Hosts that are NEVER treated as a custom domain regardless of DB state.
    SKIP_HOSTS = {"localhost", "127.0.0.1", "testserver"}

    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower().strip()
        if not host or host in self.SKIP_HOSTS:
            return await call_next(request)

        from app.database import SessionLocal
        from app.models import Bucket

        db = SessionLocal()
        try:
            bucket = db.query(Bucket).filter(Bucket.custom_domain == host).first()
            if bucket:
                request.state.bucket_id = str(bucket.id)
                request.state.bucket_slug = bucket.slug
                request.state.bucket_theme = bucket.theme_json
        # Rationale: bucket domain lookup failure must not break the request; log and continue
        except Exception as e:  # noqa: BLE001
            logger.warning("BucketHostMiddleware lookup failed for host=%s: %s", host, e)
        finally:
            db.close()
        return await call_next(request)
