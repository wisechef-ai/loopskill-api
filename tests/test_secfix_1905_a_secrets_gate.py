"""secfix_1905 Phase A — Issue #1 + #4: Boot-time secrets gate tests.

Tests:
  - PoV: default JWT_SECRET boots in non-sqlite prod → MUST raise (fails before fix)
  - fix: after _assert_production_secrets(), default secrets raise RuntimeError
  - sqlite env tolerates defaults (dev convenience)
  - OAUTH_REDIRECT_BASE missing in prod raises RuntimeError
  - All secrets set → boots cleanly
"""
import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_prod_settings(**overrides):
    """Instantiate Settings with a non-sqlite DATABASE_URL + clean secrets.

    Accepts keyword overrides so individual tests can inject bad values.
    Passes _env_file=None to avoid picking up the repo's .env file.
    We also pass explicit COOKIES_SECURE=True so the root conftest's
    WR_COOKIES_SECURE=false env var doesn't leak in.
    """
    from app.config import Settings

    defaults = dict(
        DATABASE_URL="postgresql://wisechef@localhost/wiserecipes_test",
        API_KEY="rec_prod_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
        SIGNING_SECRET="wr-tarball-signing-secret-PRODUCTION-OK",
        JWT_SECRET="wr-jwt-secret-PRODUCTION-OK",
        HEARTBEAT_PEPPER="wr-fleet-pepper-PRODUCTION-OK",
        OAUTH_REDIRECT_BASE="https://recipes.wisechef.ai",
        COOKIES_SECURE=True,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


# ── PoV commit tests (must FAIL on main, PASS after fix) ──────────────────

def test_pov_default_jwt_secret_raises_in_prod():
    """PROOF OF VULNERABILITY: Settings with default JWT_SECRET + postgres URL
    must raise RuntimeError, but on unfixed code it boots silently.

    Expected to FAIL on main (no gate exists yet).
    Expected to PASS after fix (_assert_production_secrets added).
    """
    from app.config import Settings

    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql://wisechef@localhost/wiserecipes",
            JWT_SECRET="wr-jwt-secret-change-me",
            API_KEY="rec_prod_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            SIGNING_SECRET="wr-tarball-signing-secret-PRODUCTION-OK",
            HEARTBEAT_PEPPER="wr-fleet-pepper-PRODUCTION-OK",
            OAUTH_REDIRECT_BASE="https://recipes.wisechef.ai",
            COOKIES_SECURE=True,
        )


def test_pov_default_api_key_raises_in_prod():
    """PROOF OF VULNERABILITY: default API_KEY must raise in prod env."""
    from app.config import Settings

    with pytest.raises(RuntimeError, match="API_KEY"):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql://wisechef@localhost/wiserecipes",
            API_KEY="rec_dev_wiserecipes_local_testing_key",
            SIGNING_SECRET="wr-tarball-signing-secret-PRODUCTION-OK",
            JWT_SECRET="wr-jwt-secret-PRODUCTION-OK",
            HEARTBEAT_PEPPER="wr-fleet-pepper-PRODUCTION-OK",
            OAUTH_REDIRECT_BASE="https://recipes.wisechef.ai",
            COOKIES_SECURE=True,
        )


def test_pov_default_signing_secret_raises_in_prod():
    """PROOF OF VULNERABILITY: default SIGNING_SECRET must raise in prod env."""
    from app.config import Settings

    with pytest.raises(RuntimeError, match="SIGNING_SECRET"):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql://wisechef@localhost/wiserecipes",
            API_KEY="rec_prod_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            SIGNING_SECRET="wr-tarball-signing-secret-change-me",
            JWT_SECRET="wr-jwt-secret-PRODUCTION-OK",
            HEARTBEAT_PEPPER="wr-fleet-pepper-PRODUCTION-OK",
            OAUTH_REDIRECT_BASE="https://recipes.wisechef.ai",
            COOKIES_SECURE=True,
        )


def test_pov_default_heartbeat_pepper_raises_in_prod():
    """PROOF OF VULNERABILITY: default HEARTBEAT_PEPPER must raise in prod env."""
    from app.config import Settings

    with pytest.raises(RuntimeError, match="HEARTBEAT_PEPPER"):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql://wisechef@localhost/wiserecipes",
            API_KEY="rec_prod_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            SIGNING_SECRET="wr-tarball-signing-secret-PRODUCTION-OK",
            JWT_SECRET="wr-jwt-secret-PRODUCTION-OK",
            HEARTBEAT_PEPPER="wr-fleet-pepper-change-me",
            OAUTH_REDIRECT_BASE="https://recipes.wisechef.ai",
            COOKIES_SECURE=True,
        )


# ── Issue #4 (OAUTH_REDIRECT_BASE required in prod) ──────────────────────

def test_pov_missing_oauth_redirect_base_raises_in_prod():
    """PROOF OF VULNERABILITY: empty OAUTH_REDIRECT_BASE in prod must raise."""
    from app.config import Settings

    with pytest.raises(RuntimeError, match="OAUTH_REDIRECT_BASE"):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql://wisechef@localhost/wiserecipes",
            API_KEY="rec_prod_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            SIGNING_SECRET="wr-tarball-signing-secret-PRODUCTION-OK",
            JWT_SECRET="wr-jwt-secret-PRODUCTION-OK",
            HEARTBEAT_PEPPER="wr-fleet-pepper-PRODUCTION-OK",
            OAUTH_REDIRECT_BASE="",
            COOKIES_SECURE=True,
        )


def test_pov_http_oauth_redirect_base_raises_in_prod():
    """PROOF OF VULNERABILITY: http:// OAUTH_REDIRECT_BASE in prod must raise."""
    from app.config import Settings

    with pytest.raises(RuntimeError, match="OAUTH_REDIRECT_BASE"):
        Settings(
            _env_file=None,
            DATABASE_URL="postgresql://wisechef@localhost/wiserecipes",
            API_KEY="rec_prod_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            SIGNING_SECRET="wr-tarball-signing-secret-PRODUCTION-OK",
            JWT_SECRET="wr-jwt-secret-PRODUCTION-OK",
            HEARTBEAT_PEPPER="wr-fleet-pepper-PRODUCTION-OK",
            OAUTH_REDIRECT_BASE="http://recipes.wisechef.ai",
            COOKIES_SECURE=True,
        )


# ── Passing cases (green after fix, AND must also pass before fix) ────────

def test_sqlite_env_tolerates_defaults():
    """SQLite (dev) env should NOT raise even with default change-me values."""
    from app.config import Settings

    # This should NOT raise — dev convenience
    s = Settings(
        _env_file=None,
        DATABASE_URL="sqlite:///./test.db",
        API_KEY="rec_dev_wiserecipes_local_testing_key",
        SIGNING_SECRET="wr-tarball-signing-secret-change-me",
        JWT_SECRET="wr-jwt-secret-change-me",
        HEARTBEAT_PEPPER="wr-fleet-pepper-change-me",
        OAUTH_REDIRECT_BASE="",
        COOKIES_SECURE=False,
    )
    assert "sqlite" in s.DATABASE_URL


def test_prod_settings_with_all_secrets_set_boots_cleanly():
    """All secrets set + https OAUTH_REDIRECT_BASE → no RuntimeError."""
    s = make_prod_settings()
    assert s.DATABASE_URL.startswith("postgresql://")
    assert s.JWT_SECRET == "wr-jwt-secret-PRODUCTION-OK"
    assert s.OAUTH_REDIRECT_BASE == "https://recipes.wisechef.ai"
