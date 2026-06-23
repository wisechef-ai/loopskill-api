"""GitHub & Google OAuth authentication for WiseRecipes creators.

Flow:
1. Creator clicks "Sign in with GitHub/Google" -> redirects to OAuth provider
2. Provider redirects back with code -> exchange for access token
3. Fetch user profile -> create/update User record
4. Issue JWT for API authentication
"""

import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User

logger = logging.getLogger(__name__)

# ── GitHub OAuth URLs ───────────────────────────────────────────────────────
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_API_URL = "https://api.github.com/user"
GITHUB_EMAIL_URL = "https://api.github.com/user/emails"

# ── Google OAuth URLs ──────────────────────────────────────────────────────
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_API_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


class AuthError(Exception):
    """Raised on authentication failures."""

    pass


# ── GitHub helpers ───────────────────────────────────────────────────────


def get_github_auth_url(state: str, redirect_uri: str) -> str:
    """Build the GitHub OAuth authorization URL."""
    params = {
        "client_id": settings.GITHUB_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "read:user user:email",
        "state": state,
    }
    return f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_github_code(code: str) -> dict:
    """Exchange a GitHub OAuth code for user profile data."""
    if not settings.GITHUB_CLIENT_ID or not settings.GITHUB_CLIENT_SECRET:
        raise AuthError("GitHub OAuth not configured (WR_GITHUB_CLIENT_ID/SECRET missing)")

    async with httpx.AsyncClient(timeout=30) as client:
        # Exchange code for access token
        token_resp = await client.post(
            GITHUB_TOKEN_URL,
            json={
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code != 200:
            raise AuthError(f"GitHub token exchange failed: {token_resp.status_code}")

        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise AuthError(f"No access token in GitHub response: {token_data}")

        # Fetch user profile
        user_resp = await client.get(
            GITHUB_API_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            raise AuthError(f"GitHub profile fetch failed: {user_resp.status_code}")

        profile = user_resp.json()

        # Fetch primary email (if not public)
        email = profile.get("email")
        if not email:
            email_resp = await client.get(
                GITHUB_EMAIL_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if email_resp.status_code == 200:
                emails = email_resp.json()
                primary = next(
                    (e for e in emails if e.get("primary") and e.get("verified")),
                    None,
                )
                if primary:
                    email = primary["email"]

        return {
            "provider": "github",
            "github_id": profile["id"],
            "username": profile.get("login", ""),
            "display_name": profile.get("name") or profile.get("login", ""),
            "email": email,
            "avatar_url": profile.get("avatar_url"),
        }


# ── Google helpers ───────────────────────────────────────────────────────


def get_google_auth_url(state: str, redirect_uri: str) -> str:
    """Build the Google OAuth authorization URL."""
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": "openid email profile",
        "response_type": "code",
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_google_code(code: str) -> dict:
    """Exchange a Google OAuth code for user profile data."""
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise AuthError("Google OAuth not configured (WR_GOOGLE_CLIENT_ID/SECRET missing)")

    async with httpx.AsyncClient(timeout=30) as client:
        # Exchange code for access token
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": f"{settings.OAUTH_REDIRECT_BASE}/api/auth/google/callback",
            },
            headers={"Accept": "application/json"},
        )
        if token_resp.status_code != 200:
            raise AuthError(f"Google token exchange failed: {token_resp.status_code}")

        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise AuthError(f"No access token in Google response: {token_data}")

        # Fetch user profile
        user_resp = await client.get(
            GOOGLE_API_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            raise AuthError(f"Google profile fetch failed: {user_resp.status_code}")

        profile = user_resp.json()

        return {
            "provider": "google",
            "google_id": profile.get("id"),
            "display_name": profile.get("name", ""),
            "email": profile.get("email"),
            "avatar_url": profile.get("picture"),
        }


# ── User persistence ─────────────────────────────────────────────────────


def find_or_create_user(db: Session, github_data: dict) -> User:
    """Backward-compatible alias for find_or_create_user_by_github."""
    return find_or_create_user_by_github(db, github_data)


def find_or_create_user_by_github(db: Session, github_data: dict) -> User:
    """Find existing user by GitHub ID or create a new one."""
    github_id = github_data["github_id"]

    user = db.query(User).filter(User.github_id == github_id).first()
    if user:
        # Update fields
        user.display_name = github_data["display_name"]
        user.email = user.email or github_data.get("email")
        user.avatar_url = github_data.get("avatar_url")
        db.commit()
        db.refresh(user)
        return user

    # Create new user
    user = User(
        id=uuid4(),
        github_id=github_id,
        email=github_data.get("email"),
        display_name=github_data["display_name"],
        avatar_url=github_data.get("avatar_url"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(f"Created new user {user.id} via GitHub OAuth ({github_data['username']})")
    return user


def find_or_create_user_by_google(db: Session, google_data: dict) -> User:
    """Find existing user by Google ID or create a new one."""
    google_id = google_data["google_id"]

    user = db.query(User).filter(User.google_id == google_id).first()
    if user:
        # Update fields
        user.display_name = google_data["display_name"]
        user.email = user.email or google_data.get("email")
        user.avatar_url = google_data.get("avatar_url")
        db.commit()
        db.refresh(user)
        return user

    # Create new user
    user = User(
        id=uuid4(),
        google_id=google_id,
        email=google_data.get("email"),
        display_name=google_data["display_name"],
        avatar_url=google_data.get("avatar_url"),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(f"Created new user {user.id} via Google OAuth ({google_data.get('email')})")
    return user


# ── JWT management ───────────────────────────────────────────────────────


def _jwt_keys() -> dict[str, str]:
    """Return the multi-key dict from JWT_KEYS, or {} if not configured."""
    raw = getattr(settings, "JWT_KEYS", None)
    if not raw:
        return {}
    import json as _json

    try:
        parsed = _json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except (ValueError, TypeError):
        pass
    return {}


def create_jwt(user: User) -> str:
    """Issue a JWT for the given user.

    Multi-key mode (G.3): when JWT_KEYS (JSON dict kid→secret) and
    JWT_ACTIVE_KID are both set, the token is signed with the active key and
    a ``kid`` header is included for transparent rotation.

    Legacy mode: when those settings are absent, behaviour is identical to
    the pre-rotation implementation (plain JWT_SECRET, no kid header).
    """
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "exp": datetime.now(UTC) + timedelta(hours=settings.JWT_EXPIRATION_HOURS),
        "iat": datetime.now(UTC),
        "iss": "wiserecipes",
    }
    keys = _jwt_keys()
    active_kid = getattr(settings, "JWT_ACTIVE_KID", None)
    if keys and active_kid and active_kid in keys:
        # Multi-key path: sign with the active kid and embed kid in header.
        return jwt.encode(
            payload,
            keys[active_kid],
            algorithm=settings.JWT_ALGORITHM,
            headers={"kid": active_kid},
        )
    # Legacy path: identical to pre-rotation behaviour.
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_jwt(token: str) -> dict | None:
    """Verify and decode a JWT. Returns payload or None.

    Multi-key mode (G.3): if the token carries a ``kid`` header and JWT_KEYS
    contains that kid, verification is attempted with that key first.  Falls
    back to JWT_SECRET so tokens issued before rotation remain valid.

    Legacy mode: when JWT_KEYS is not configured the function is identical to
    the pre-rotation implementation.
    """
    try:
        # Decode header without verification to extract kid (if present).
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
    except jwt.InvalidTokenError:
        return None

    keys = _jwt_keys()
    secrets_to_try: list[str] = []
    if kid and kid in keys:
        # Preferred: verify with the key that signed this specific token.
        secrets_to_try.append(keys[kid])
    # Always append the legacy secret as a fallback so tokens signed before
    # JWT_KEYS was populated continue to verify successfully.
    secrets_to_try.append(settings.JWT_SECRET)

    for secret in secrets_to_try:
        try:
            payload = jwt.decode(
                token,
                secret,
                algorithms=[settings.JWT_ALGORITHM],
                options={"require": ["sub", "exp", "iss"]},
            )
            if payload.get("iss") != "wiserecipes":
                return None
            return payload
        except jwt.ExpiredSignatureError:
            # Expired is definitive — no point trying other secrets.
            return None
        except jwt.InvalidTokenError:
            continue  # wrong key or malformed — try next

    return None


def get_user_from_jwt(db: Session, token: str) -> User | None:
    """Verify JWT and return the User object."""
    payload = verify_jwt(token)
    if not payload:
        return None

    from uuid import UUID

    try:
        user_id = UUID(payload["sub"])
    except (ValueError, KeyError):
        return None

    return db.query(User).filter(User.id == user_id).first()
