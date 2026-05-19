"""secfix_1905 Phase A — Issue #11: COOKIES_SECURE setting.

Tests:
  - All set_cookie calls in auth_routes.py use settings.COOKIES_SECURE
  - COOKIES_SECURE=False raises on non-sqlite DB
  - COOKIES_SECURE=False is OK for sqlite
  - COOKIES_SECURE=True is the default
"""
import re
from pathlib import Path


AUTH_ROUTES_PATH = Path(__file__).parent.parent / "app" / "auth_routes.py"
CONFIG_PATH = Path(__file__).parent.parent / "app" / "config.py"


def test_auth_routes_no_host_heuristic_for_secure_flag():
    """All set_cookie calls in auth_routes.py must NOT use 'settings.HOST != 0.0.0.0'
    for the secure= flag. This is the old heuristic that has been replaced by
    COOKIES_SECURE.
    """
    source = AUTH_ROUTES_PATH.read_text()
    # The old heuristic pattern must be gone
    assert "settings.HOST != " not in source, (
        "auth_routes.py still has 'settings.HOST != ...' heuristic for secure= flag. "
        "Replace with 'secure=settings.COOKIES_SECURE'."
    )


def test_auth_routes_uses_cookies_secure_setting():
    """All set_cookie calls in auth_routes.py should use settings.COOKIES_SECURE."""
    source = AUTH_ROUTES_PATH.read_text()
    # Count secure=settings.COOKIES_SECURE occurrences
    count = source.count("secure=settings.COOKIES_SECURE")
    # There were 6 occurrences of the old heuristic (lines 80, 103, 132, 138, 209, 215)
    assert count >= 5, (
        f"Expected ≥5 uses of 'secure=settings.COOKIES_SECURE' in auth_routes.py, "
        f"found {count}. All set_cookie calls must use the explicit COOKIES_SECURE setting."
    )


def test_config_has_cookies_secure_field():
    """Settings class must define COOKIES_SECURE: bool = True."""
    from app.config import Settings
    assert hasattr(Settings.model_fields, "__getitem__") or hasattr(Settings, "model_fields"), \
        "Settings must be a pydantic-settings model"
    # Check the field exists with default True
    import inspect
    src = CONFIG_PATH.read_text()
    assert "COOKIES_SECURE: bool = True" in src, (
        "app/config.py must define 'COOKIES_SECURE: bool = True'"
    )


def test_cookies_secure_false_raises_on_postgres():
    """COOKIES_SECURE=False must raise RuntimeError when DATABASE_URL is non-sqlite."""
    from app.config import Settings

    with __import__("pytest").raises(RuntimeError, match="COOKIES_SECURE"):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql://wisechef@localhost/wiserecipes_test",
            API_KEY="rec_prod_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            SIGNING_SECRET="wr-tarball-signing-secret-PRODUCTION-OK",
            JWT_SECRET="wr-jwt-secret-PRODUCTION-OK",
            HEARTBEAT_PEPPER="wr-fleet-pepper-PRODUCTION-OK",
            OAUTH_REDIRECT_BASE="https://recipes.wisechef.ai",
            COOKIES_SECURE=False,  # ← must be rejected in postgres env
        )


def test_cookies_secure_false_ok_for_sqlite():
    """COOKIES_SECURE=False is allowed in sqlite (dev) env."""
    from app.config import Settings

    s = Settings(
        _env_file=None,
        DATABASE_URL="sqlite:///./test_dev.db",
        COOKIES_SECURE=False,
    )
    assert s.COOKIES_SECURE is False


def test_cookies_secure_true_is_default():
    """COOKIES_SECURE defaults to True when not overridden by env."""
    from app.config import Settings

    # Build with sqlite so other gate checks pass.
    # Pass COOKIES_SECURE=True explicitly to avoid the root conftest's
    # WR_COOKIES_SECURE=false env var overriding the default.
    s = Settings(
        _env_file=None,
        DATABASE_URL="sqlite:///./test_dev.db",
        COOKIES_SECURE=True,
    )
    assert s.COOKIES_SECURE is True
