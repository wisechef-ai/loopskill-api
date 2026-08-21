"""flywheel Phase-1 (F1.1) — loopskill_publish_bundle MCP tool.

Closes the compose→publish dead-end (verified live 2026-08-19/21):
``loopskill_compose_bundle_from_links`` creates a new PRIVATE bundle and
tells the caller to "publish it to get a shareable bundle:// link" — but
until this tool existed, publishing was REST-only
(``PATCH /api/cookbooks/{id}/visibility``). A pure-MCP agent-creator with no
REST client had no way to finish the loop it was just told to finish.
Composite loops got a full MCP mirror (``loopskill_publish_composite_loop``,
activate_0701 Phase A2); bundles never did. 0 third-party bundles had ever
been published as of this fix.

Mirrors ``app/mcp/tools/composite_loop_catalog.py:loopskill_publish_composite_loop``'s
auth/ownership/error conventions (fail-closed 401/404, dispatch-chain
delegation via a local ``_NOT_HANDLED`` sentinel), but reuses the SAME
visibility write path REST already uses
(``app/bundle_routes.py:set_cookbook_visibility``) — ``_ensure_bundle_slug``,
the ``Bundle.visibility`` ORM validator (liked-bundle guard + slug-mint),
``_touch_bundle_generation`` — so the two surfaces can never diverge on what
"published" means for a bundle. Ownership is checked via
``authz.can_write_cookbook`` (tenant-scoped owner-or-master, same predicate
``loopskill_harvest``/``loopskill_configure_feedback`` already use for bundle
writes over MCP), not re-derived.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app import authz
from app.auth_ctx import AuthContext
from app.bundle_routes import _ensure_bundle_slug, _touch_bundle_generation
from app.mcp.tools.fleet_write import _NOT_HANDLED  # shared dispatch-chain sentinel
from app.models import Bundle, LikedBundleNotPublishableError


def _resolve_bundle(db: Session, bundle_id: str) -> Bundle | None:
    try:
        bid = UUID(bundle_id)
    except (ValueError, AttributeError, TypeError):
        return None
    return db.query(Bundle).filter(Bundle.id == bid).first()


def loopskill_publish_bundle(
    db: Session,
    *,
    bundle_id: str,
    ctx: AuthContext | None = None,
) -> dict[str, Any]:
    """Publish (make public) a bundle the caller owns — the MCP mirror of
    ``PATCH /api/cookbooks/{id}/visibility`` with ``visibility='public'``.

    Auth: fail-closed.
      - Anonymous/no ctx -> ``{"error": "auth_required", "status": 401}``.
      - Non-owner (and non-master) -> ``{"error": "bundle_not_found", "status": 404}``.
        No 403-vs-404 oracle: a caller who doesn't own the bundle gets the
        same answer as a nonexistent bundle_id (mirrors
        ``loopskill_harvest``'s mesh_0408 W1b fix and
        ``loopskill_configure_feedback``'s ownership gate).
      - Ownership check is ``authz.can_write_cookbook`` — master always,
        tenant-scoped owner match otherwise, bundle-scoped keys restricted to
        their one bundle. Fails closed on anything else.

    Idempotent: publishing an already-public bundle returns
    ``{"published": True, "transition": "already_public", ...}`` with the
    CURRENT state — not an error, and no redundant write (the DB is only
    touched on an actual private->public transition).

    Explicit visibility in the response: ``visibility`` is always the
    resulting value ('public' on success), ``was_public`` states whether this
    call actually flipped it, and ``transition`` spells out
    'private_to_public' vs 'already_public' so a calling agent never has to
    infer the state change from a diff.

    Tier gates: per D-011 (app/services/bundle_quota.py), PUBLIC bundles are
    UNLIMITED on every tier including free — only the PRIVATE-bundle cap is
    metered, and it is enforced elsewhere (at compose/create time). This tool
    performs NO quota check on the private->public transition and is
    deliberately never pro-gated, matching the REST route it mirrors.
    """
    if ctx is None or ctx.scope in (None, "anonymous"):
        return {"error": "auth_required", "status": 401}

    bundle = _resolve_bundle(db, bundle_id)
    if bundle is None:
        return {"error": "bundle_not_found", "status": 404}
    if not authz.can_write_cookbook(ctx, bundle):
        # mesh_0408 W1b precedent (loopskill_harvest, loopskill_configure_feedback):
        # the SAME answer the absent case gives one line up — 403-vs-404 would
        # confirm the bundle id is real to a non-owner; collapse to one answer.
        return {"error": "bundle_not_found", "status": 404}

    was_public = bundle.visibility == "public"

    if not was_public:
        try:
            # Triggers Bundle._validate_visibility: rejects the system Liked
            # bundle, mints a slug in-place if one is missing yet (idempotent —
            # never changes an existing slug).
            bundle.visibility = "public"
        except LikedBundleNotPublishableError as exc:
            db.rollback()
            return {"error": "liked_bundle_is_private", "status": 422, "detail": str(exc)}

        # fix/bundle-slug-on-create parity (app/bundle_routes.py:set_cookbook_visibility):
        # belt-and-suspenders — the validator above already mints a slug on the
        # public transition, but pre-fix rows that predate the validator (if
        # any survive) still get one here.
        _ensure_bundle_slug(db, bundle)
        _touch_bundle_generation(db, bundle.id)
        db.commit()
        db.refresh(bundle)

    return {
        "bundle_id": str(bundle.id),
        "slug": bundle.slug,
        "visibility": bundle.visibility,
        "was_public": was_public,
        "published": True,
        "transition": "already_public" if was_public else "private_to_public",
        "bundle_url": f"bundle://{bundle.slug}" if bundle.slug else None,
    }


def dispatch_bundle_publish(
    name: str,
    db: Session,
    args: dict[str, Any],
    ctx: AuthContext | None = None,
) -> Any:
    """Delegated dispatch for F1.1 — see app/mcp/dispatch_chain.py.

    Returns _NOT_HANDLED if ``name`` is not the publish-bundle tool.
    """
    if name == "loopskill_publish_bundle":
        return loopskill_publish_bundle(db, bundle_id=args["bundle_id"], ctx=ctx)
    return _NOT_HANDLED
