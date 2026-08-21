"""flywheel Phase-1 (F1.3) — GET /api/creators/me/stats.

Minimal creator stats surface: per-bundle install counts for the bundles the
caller owns. Per council direction this is deliberately boring and additive —
no digest, no follows.

Auth: user-scoped only (JWT wr_jwt cookie OR a real x-api-key). Mirrors the
resolution the rest of the bundle-facing surface uses
(``request.state.auth_ctx``, stamped by ``APIKeyMiddleware`` for both auth
methods — see app/middleware/api_key.py and app/middleware/_jwt_cookie_auth.py).
Anonymous callers get 401. This path is intentionally NOT added to
``JWT_AUTH_PREFIXES`` (that would make x-api-key callers 401 by skipping key
validation entirely) nor to ``PUBLIC_PREFIXES`` (this is not a public read
surface) — it rides the DEFAULT middleware path, same as bundle_routes.py.

INTERNAL-EXCLUSION rule (documented per the task spec — this is the load-
bearing part of the contract, not an implementation detail):
  The ONLY existing signal in this codebase for "this traffic is
  internal/test/CI/fleet harness traffic, not a real third-party install" is
  ``APIKey.is_test`` (see app/billable_units.py:SYNTHETIC_MARKER, and its use
  in app/_skill_helpers.py:_install_counts_for /
  _cookbook_install_counts for the exact same organic-vs-synthetic split on
  every other public install-count surface — discovery ranking, leaderboards,
  the cookbook card's installs_total/installs_7d signal). There is no
  separate "rec_fleet_" install marker: fleet member keys are ordinary
  ``rec_`` API keys minted per-agent (app/fleet_member_routes.py), stamped
  ``is_test`` from the ENROLLING key's own ``is_test`` flag at member-mint
  time (same precedent cited in app/fleet_routes.py's api_key_id comment) —
  so ``is_test`` is already the transitive internal-fleet marker, not just a
  literal-key-prefix check. This route reuses that exact predicate rather
  than inventing a second, divergent notion of "internal": an install is
  counted as external (creator-facing, genuine signal) when its
  ``InstallEvent.api_key_id`` is NULL (anonymous — always organic) OR the
  owning ``APIKey.is_test`` is false/NULL; it is excluded from
  ``installs_external`` (but still counted in ``installs_total``, the raw,
  unfiltered count) when ``api_keys.is_test`` is true.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app import authz
from app.database import get_db
from app.models import APIKey, Bundle, InstallEvent

router = APIRouter(prefix="/api/creators", tags=["creators"])


def _resolve_stats_ctx(request: Request):
    """Resolve the authenticated caller for this route, or 401.

    Reads ``request.state.auth_ctx`` directly (the PUBLIC_PREFIXES-adjacent
    pattern documented in the loopskill-api-endpoint-development skill: "On a
    path NOT gated by JWT_AUTH_PREFIXES, read request.state.auth_ctx and 401
    if it's None/anonymous" — this route sits on the default APIKeyMiddleware
    path, same class as composite_loop_routes.publish_composite_loop).
    """
    ctx = getattr(request.state, "auth_ctx", None)
    scope = getattr(ctx, "scope", None)
    if ctx is None or scope in (None, "anonymous", "unauthorized"):
        raise HTTPException(status_code=401, detail="auth_required")
    if scope not in ("user", "master"):
        # cbt_/fleet-scoped tokens are not a creator identity.
        raise HTTPException(status_code=401, detail="auth_required")
    return ctx


@router.get("/me/stats")
def get_creator_stats(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Per-bundle install counts for bundles the caller owns.

    Response:
        {
          "bundles": [
            {"slug": ..., "title": ..., "visibility": ...,
             "installs_total": <int>, "installs_external": <int>},
            ...
          ],
          "generated_at": "<iso8601>",
          "internal_exclusion_rule": "<human-readable, see module docstring>",
        }

    Empty state (caller owns zero bundles, or a master caller with no owned
    rows) returns ``{"bundles": [], ...}`` — never 404 or 500.
    """
    ctx = _resolve_stats_ctx(request)

    if ctx.scope == "master":
        # Master has no personal bundle set; an explicit empty result is more
        # honest than either 403 or dumping every creator's bundles.
        owned = []
    else:
        owned = (
            db.query(Bundle)
            .filter(authz.owner_match_within_tenant_clause(ctx, Bundle))
            .order_by(Bundle.created_at.asc())
            .all()
        )

    bundle_ids = [b.id for b in owned]

    # One grouped query for every owned bundle — never a per-bundle loop
    # (perf discipline, loopskill-api-endpoint-development skill).
    counts_by_bundle: dict[Any, tuple[int, int]] = {}
    if bundle_ids:
        # Same organic/synthetic split as app/_skill_helpers.py:_install_counts_for
        # (case-based sum, not a filtered second query — one round trip).
        rows = (
            db.query(
                InstallEvent.bundle_id,
                func.count(InstallEvent.id).label("total"),
                func.sum(case((func.coalesce(APIKey.is_test, False).is_(False), 1), else_=0)).label(
                    "external"
                ),
            )
            .outerjoin(APIKey, APIKey.id == InstallEvent.api_key_id)
            .filter(InstallEvent.bundle_id.in_(bundle_ids))
            .group_by(InstallEvent.bundle_id)
            .all()
        )
        counts_by_bundle = {bid: (int(total or 0), int(external or 0)) for bid, total, external in rows}

    bundles_out = []
    for b in owned:
        total, external = counts_by_bundle.get(b.id, (0, 0))
        bundles_out.append(
            {
                "slug": b.slug,
                "title": b.name,
                "visibility": b.visibility,
                "installs_total": total,
                "installs_external": external,
            }
        )

    return {
        "bundles": bundles_out,
        "generated_at": datetime.now(UTC).isoformat(),
        "internal_exclusion_rule": (
            "installs_external excludes InstallEvent rows whose api_key_id "
            "resolves to an APIKey with is_test=true (the codebase-wide "
            "internal/test/CI/fleet-harness marker — see "
            "app/billable_units.py:SYNTHETIC_MARKER); anonymous installs "
            "(api_key_id IS NULL) are always counted as external. "
            "installs_total is the raw, unfiltered count."
        ),
    }
