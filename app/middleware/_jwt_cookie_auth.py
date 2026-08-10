"""JWT-cookie authentication helpers for :mod:`app.middleware.api_key`.

Extracted from ``api_key.py`` (fdeloop_0808 Phase D) for the same reason
``_org_scope`` and ``_public_paths`` were before it: adding the public
plugin.json exemption pushed that module from 599 to 605 lines, past the
600-line god-object cap enforced by
``tests/test_w0_2_pyfile_size_discipline.py``.

The precedent is deliberate — qa0208-w3 split ``key_prefixes.py`` rather than
take a waiver. These two functions are a coherent unit (portal/OAuth users
carry a ``wr_jwt`` cookie instead of an ``x-api-key`` header) with no module-
level dependencies: every import they need is already function-local to avoid
import cycles, so the move is mechanical and behaviour-preserving.

Re-exported from ``api_key`` under the historical private names so existing
callers and test patches keep working.
"""

# mesh_0408 W1: tenant resolution lives in _org_scope. Imported under the
# historical private name because the extracted body below calls it directly.
from typing import TYPE_CHECKING

from app.middleware._org_scope import resolve_org_membership as _resolve_org_membership

if TYPE_CHECKING:  # pragma: no cover — annotation-only, avoids an import cycle
    from app.auth_ctx import AuthContext


def auth_ctx_from_jwt_cookie(request) -> "AuthContext":
    """Return an AuthContext populated from the wr_jwt cookie / Bearer token.

    Used on public skill-detail GETs where no x-api-key is present.  If the
    cookie is absent or invalid, returns AuthContext.anonymous() so downstream
    handlers always have a valid auth_ctx to inspect.

    Resolution order:
      1. ``wr_jwt`` cookie (browser portal sessions)
      2. ``Authorization: Bearer <token>`` (SPA clients, backward compat)

    Issue #25 (secfix_1905/H): extracted from the deleted _resolve_caller_tier
    helper so that JWT-cookie callers on public routes are properly hydrated
    into auth_ctx without the route needing a separate DB call.
    """
    from app.auth_ctx import AuthContext

    token = request.cookies.get("wr_jwt")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        return AuthContext.anonymous()

    try:
        from app.auth_routes import verify_jwt  # local import to avoid cycles

        payload = verify_jwt(token)
    # Rationale: any JWT validation failure must not crash public skill-detail — return anonymous
    except Exception:  # noqa: BLE001
        return AuthContext.anonymous()

    if not payload:
        return AuthContext.anonymous()

    from uuid import UUID

    try:
        user_id = UUID(payload["sub"])
    except (ValueError, KeyError, TypeError):
        return AuthContext.anonymous()

    from app.database import SessionLocal
    from app.models import User

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        # BUGFIX (2026-07-19): identity mustn't gate on subscription status
        # (old code 401'd fresh signups). `tier` alone stays conditional.
        if user:
            org_id, is_org_owner = _resolve_org_membership(db, user_id)
            tier = user.subscription_tier if user.subscription_status in ("active", "trialing") else None
            return AuthContext(
                scope="user", user_id=user_id, tier=tier, org_id=org_id, is_org_owner=is_org_owner
            )
    finally:
        db.close()

    return AuthContext.anonymous()


def try_jwt_cookie_auth(request) -> bool:
    """Authenticate an authed route via the wr_jwt cookie. Returns success.

    Portal/OAuth users carry a ``wr_jwt`` cookie, not an ``x-api-key`` header.
    On a valid user-scope cookie, stamp ``auth_ctx`` + ``api_key_user_id`` so
    cookie auth and key auth converge (cookbook routes read api_key_user_id).
    The id is the real user UUID (never ``None``), so admin routes still reject.
    """
    jwt_ctx = auth_ctx_from_jwt_cookie(request)
    if jwt_ctx is not None and getattr(jwt_ctx, "scope", None) == "user":
        request.state.auth_ctx = jwt_ctx
        request.state.api_key_user_id = jwt_ctx.user_id
        request.state.api_key_id = None
        return True
    return False
