"""Auth router — GitHub & Google OAuth endpoints for WiseRecipes.

Endpoints:
  GET  /api/auth/github/login    — redirect to GitHub OAuth
  GET  /api/auth/github/callback — handle GitHub OAuth callback
  GET  /api/auth/google/login    — redirect to Google OAuth
  GET  /api/auth/google/callback — handle Google OAuth callback
  GET  /api/auth/me              — return current user from JWT cookie
"""

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import (
    AuthError,
    create_jwt,
    exchange_github_code,
    exchange_google_code,
    find_or_create_user_by_github,
    find_or_create_user_by_google,
    get_github_auth_url,
    get_google_auth_url,
    verify_jwt,
)
from app.tier_labels import _is_operator_tier, _is_paid_tier
from app.config import settings
from app.database import get_db
from app.referral import (
    REFERRAL_COOKIE_MAX_AGE,
    REFERRAL_COOKIE_NAME,
    ensure_referral_code,
    process_referral_cookie,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

JWT_COOKIE_NAME = "wr_jwt"
JWT_COOKIE_MAX_AGE = settings.JWT_EXPIRATION_HOURS * 3600


def _build_redirect_uri(request: Request, provider: str) -> str:
    """Build the OAuth redirect URI from the incoming request."""
    # Prefer configured base URL, fall back to request origin
    base_url = getattr(settings, "OAUTH_REDIRECT_BASE", "").rstrip("/")
    if base_url:
        return f"{base_url}/api/auth/{provider}/callback"
    # Derive from request
    scheme = request.url.scheme
    host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}/api/auth/{provider}/callback"


def _make_success_redirect(jwt_token: str, next_url: str | None = None) -> RedirectResponse:
    """Create a redirect response with JWT cookie set.

    next_url: if provided AND starts with `/api/` or `/library` or `/skills/`,
    redirect there after setting cookie (for /signin?next=... flows).
    Otherwise default to /library?auth=success.
    """
    SAFE_NEXT_PREFIXES = ("/api/", "/library", "/skills/", "/billing/", "/publish")
    target = "/library?auth=success"
    if next_url and any(next_url.startswith(p) for p in SAFE_NEXT_PREFIXES):
        sep = "&" if "?" in next_url else "?"
        target = f"{next_url}{sep}auth=success" if "?" in next_url else f"{next_url}?auth=success"

    response = RedirectResponse(url=target, status_code=302)
    response.set_cookie(
        key=JWT_COOKIE_NAME,
        value=jwt_token,
        max_age=JWT_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.HOST != "0.0.0.0",  # secure in prod, not local
        samesite="lax",
        path="/",
    )
    return response


def _make_error_redirect(error: str) -> RedirectResponse:
    """Create a redirect response for auth errors."""
    response = RedirectResponse(url=f"/signin?auth=error&reason={error}", status_code=302)
    return response


def _stamp_referral_cookie(response, ref: str | None) -> None:
    """If a ?ref=CODE query param was supplied at /login, persist it for 30d
    so the OAuth round-trip carries it through to the callback (WIS-660)."""
    if not ref:
        return
    response.set_cookie(
        key=REFERRAL_COOKIE_NAME,
        value=ref,
        max_age=REFERRAL_COOKIE_MAX_AGE,
        httponly=False,
        secure=settings.HOST != "0.0.0.0",
        samesite="lax",
        path="/",
    )


# ── GitHub OAuth ─────────────────────────────────────────────────────────

@router.get("/github/login")
async def github_login(request: Request, next: Optional[str] = None, ref: Optional[str] = None):
    """Initiate GitHub OAuth flow. Preserves optional `next` query param via cookie.

    Optional `ref=CODE` query param is stamped as a 30-day cookie so the
    referral attribution survives the OAuth round-trip (WIS-660).
    """
    if not settings.GITHUB_CLIENT_ID:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured")

    state = secrets.token_urlsafe(32)
    redirect_uri = _build_redirect_uri(request, "github")
    auth_url = get_github_auth_url(state=state, redirect_uri=redirect_uri)

    response = RedirectResponse(url=auth_url, status_code=302)
    # Store state in a short-lived cookie for CSRF protection
    response.set_cookie(
        key="oauth_state",
        value=state,
        max_age=600,  # 10 minutes
        httponly=True,
        secure=settings.HOST != "0.0.0.0",
        samesite="lax",
    )
    if next:
        response.set_cookie(
            key="oauth_next", value=next, max_age=600, httponly=True,
            secure=settings.HOST != "0.0.0.0", samesite="lax",
        )
    _stamp_referral_cookie(response, ref)
    return response


@router.get("/github/callback")
async def github_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Handle GitHub OAuth callback. Exchanges code, creates user, sets JWT cookie."""
    if not code:
        return _make_error_redirect("no_code")
    if not state:
        return _make_error_redirect("no_state")

    # Verify state matches (CSRF protection)
    cookie_state = request.cookies.get("oauth_state")
    if cookie_state and cookie_state != state:
        logger.warning("GitHub OAuth state mismatch")
        return _make_error_redirect("state_mismatch")

    try:
        github_data = await exchange_github_code(code)
        user = find_or_create_user_by_github(db, github_data)
        # WIS-660: capture referral attribution + give every user their own code.
        try:
            ref_code = request.cookies.get(REFERRAL_COOKIE_NAME)
            if ref_code:
                process_referral_cookie(db, user, ref_code)
            ensure_referral_code(user, db)
        except Exception:  # noqa: BLE001 — never block sign-in on referral failure
            logger.exception("Referral processing failed for user %s (non-fatal)", user.id)
        jwt_token = create_jwt(user)
        next_url = request.cookies.get("oauth_next")
        logger.info(f"GitHub auth success: user={user.id} ({user.display_name}) next={next_url!r}")
        resp = _make_success_redirect(jwt_token, next_url=next_url)
        if next_url:
            resp.delete_cookie("oauth_next", path="/")
        # Clear the referral cookie once we've persisted it; safe to drop.
        resp.delete_cookie(REFERRAL_COOKIE_NAME, path="/")
        return resp
    except AuthError as e:
        logger.error(f"GitHub auth failed: {e}")
        return _make_error_redirect("github_error")


# ── Google OAuth ─────────────────────────────────────────────────────────

@router.get("/google/login")
async def google_login(request: Request, next: Optional[str] = None, ref: Optional[str] = None):
    """Initiate Google OAuth flow. Preserves optional `next` query param via cookie.

    Optional `ref=CODE` query param is stamped as a 30-day cookie (WIS-660).
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google OAuth not configured")

    state = secrets.token_urlsafe(32)
    redirect_uri = _build_redirect_uri(request, "google")
    auth_url = get_google_auth_url(state=state, redirect_uri=redirect_uri)

    response = RedirectResponse(url=auth_url, status_code=302)
    response.set_cookie(
        key="oauth_state",
        value=state,
        max_age=600,
        httponly=True,
        secure=settings.HOST != "0.0.0.0",
        samesite="lax",
    )
    if next:
        response.set_cookie(
            key="oauth_next", value=next, max_age=600, httponly=True,
            secure=settings.HOST != "0.0.0.0", samesite="lax",
        )
    _stamp_referral_cookie(response, ref)
    return response


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Handle Google OAuth callback. Exchanges code, creates user, sets JWT cookie."""
    if not code:
        return _make_error_redirect("no_code")
    if not state:
        return _make_error_redirect("no_state")

    # Verify state matches (CSRF protection)
    cookie_state = request.cookies.get("oauth_state")
    if cookie_state and cookie_state != state:
        logger.warning("Google OAuth state mismatch")
        return _make_error_redirect("state_mismatch")

    try:
        google_data = await exchange_google_code(code)
        user = find_or_create_user_by_google(db, google_data)
        # WIS-660: capture referral attribution + give every user their own code.
        try:
            ref_code = request.cookies.get(REFERRAL_COOKIE_NAME)
            if ref_code:
                process_referral_cookie(db, user, ref_code)
            ensure_referral_code(user, db)
        except Exception:  # noqa: BLE001 — never block sign-in on referral failure
            logger.exception("Referral processing failed for user %s (non-fatal)", user.id)
        jwt_token = create_jwt(user)
        next_url = request.cookies.get("oauth_next")
        logger.info(f"Google auth success: user={user.id} ({user.display_name}) next={next_url!r}")
        resp = _make_success_redirect(jwt_token, next_url=next_url)
        if next_url:
            resp.delete_cookie("oauth_next", path="/")
        resp.delete_cookie(REFERRAL_COOKIE_NAME, path="/")
        return resp
    except AuthError as e:
        logger.error(f"Google auth failed: {e}")
        return _make_error_redirect("google_error")


# ── Current user ─────────────────────────────────────────────────────────

@router.get("/me")
async def get_me(
    request: Request,
    db: Session = Depends(get_db),
):
    """Return the current authenticated user from JWT cookie or Authorization header.

    Checks:
    1. wr_jwt cookie
    2. Authorization: Bearer *** header
    """
    token: Optional[str] = None

    # Try cookie first
    token = request.cookies.get(JWT_COOKIE_NAME)

    # Fall back to Authorization header
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = verify_jwt(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    from uuid import UUID
    try:
        user_id = UUID(payload["sub"])
    except (ValueError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid token payload")

    from app.models import User
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        # Subscription state — embedded so every page that calls /api/auth/me
        # knows the user's current plan without a second round-trip to /billing/me.
        # Fixes auth-aware UI being plan-blind across Nav.astro, pricing page,
        # /skills/{slug} install gate, and any future page that wants tier-conditional UX.
        "subscription_tier": user.subscription_tier,
        "subscription_status": user.subscription_status,
        "subscription_current_period_end": (
            user.subscription_current_period_end.isoformat()
            if user.subscription_current_period_end else None
        ),
        # WIS-902: Tier feature flags for frontend tier-conditional UX
        "features": {
            "full_catalog_install": _is_paid_tier(user.subscription_tier),
            "install_rate_limit": {
                "free": 5, "cook": 100, "operator": None, "studio": None,
            }.get(user.subscription_tier, 5),
            "recipify": _is_paid_tier(user.subscription_tier),
            "cookbook_limit": {
                "free": 0, "cook": 1, "operator": None, "studio": None,
            }.get(user.subscription_tier, 0),
            "cookbook_skill_cap": {
                "free": 0, "cook": 25, "operator": None, "studio": None,
            }.get(user.subscription_tier, 0),
            "fleet_sync": _is_operator_tier(user.subscription_tier),
            "fleet_seeker": _is_operator_tier(user.subscription_tier),
            "subrecipe_priority_resolve": _is_operator_tier(user.subscription_tier),
        },
    }


# ── Reusable dependency for other routers ────────────────────────────────

def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> Optional["User"]:
    """Resolve the authenticated user from cookie/header, or return None.

    Use as a FastAPI dependency on routes where authentication is optional or
    handled with a custom 401 message in the route body. Returns None if no
    valid JWT is present — the route is responsible for raising HTTPException
    when authentication is required.
    """
    from app.models import User
    from uuid import UUID

    token: Optional[str] = request.cookies.get(JWT_COOKIE_NAME)
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        return None

    payload = verify_jwt(token)
    if not payload:
        return None

    try:
        user_id = UUID(payload["sub"])
    except (ValueError, KeyError):
        return None

    return db.query(User).filter(User.id == user_id).first()


@router.post("/logout")
async def logout():
    """Clear the JWT cookie and any auth-related cookies. Idempotent."""
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie(JWT_COOKIE_NAME, path="/")
    response.delete_cookie("oauth_state", path="/")
    response.delete_cookie("oauth_next", path="/")
    return response
