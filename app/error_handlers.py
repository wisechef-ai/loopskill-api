"""Domain-exception → HTTP response mapping, in one registrable place.

These handlers used to be defined inline inside ``main.create_app``, which made
them invisible to any app built another way — including ``tests/_app_factory``,
the shared test-app builder that otherwise mirrors production wiring. A service
that raises a domain error would then 500 under test while returning a clean
4xx in production, which is the wrong way round for a test suite to be wrong.

Registering them from a function both builders call keeps that surface honest.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_exception_handlers(app: FastAPI) -> None:
    """Attach every domain-exception handler to ``app``."""
    # spotify_2607 Phase A (§0a) — the Liked bundle is a private SYSTEM
    # collection that can never be published. The guard lives at the ORM layer
    # (Bundle._validate_visibility, an @validates hook) so it protects
    # EVERY write path, not just PATCH /visibility — this handler just turns
    # that model-layer raise into a well-formed 422 instead of a bare 500.
    from app.models import LikedBundleNotPublishableError
    from app.services.drift_service import LockMintError

    @app.exception_handler(LikedBundleNotPublishableError)
    async def _liked_bundle_not_publishable_handler(
        _request: Request, exc: LikedBundleNotPublishableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422, content={"detail": str(exc), "reason": "liked_bundle_is_private"}
        )

    # converge_0208 P1 — a bundle mutation that would freeze an uninstallable
    # entry into the bundle lock is refused at the service layer. Surfacing it
    # as a 409 naming the slug and version is the entire point of the phase:
    # ONE actionable error for the bundle owner, in place of the silent
    # 30-minute fetch-404-and-roll-back loop the member agents were stuck in.
    @app.exception_handler(LockMintError)
    async def _lock_mint_refused_handler(_request: Request, exc: LockMintError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "reason": "unresolvable_artifact",
                "slug": exc.slug,
                "version": exc.version,
                "bundle": exc.bundle_name,
            },
        )
