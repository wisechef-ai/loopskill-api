"""secfix_1905 Phase A — Issue #2: OAuth state check fails closed.

Tests:
  - PoV: GitHub callback with state= but NO oauth_state cookie → must return 302
    to /signin?auth=error&reason=state_mismatch (FAILS on main — current code
    skips the check when cookie is absent, allowing CSRF bypass)
  - PoV: Google callback same scenario
  - After fix: both callbacks reject missing cookie with 302 error redirect
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── App setup ────────────────────────────────────────────────────────────────

def make_auth_app():
    """Create a minimal test app with just the auth_routes router."""
    from app.auth_routes import router as auth_router
    from app.database import get_db

    app = FastAPI()
    app.include_router(auth_router)
    return app


@pytest.fixture()
def auth_client(db_session):
    """TestClient wired to auth routes with db override."""
    from app.database import get_db

    app = make_auth_app()

    def override_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_db
    with TestClient(app, raise_server_exceptions=False, follow_redirects=False) as c:
        yield c


# ── PoV: GitHub callback CSRF bypass ─────────────────────────────────────────

def test_pov_github_callback_no_cookie_allows_bypass(auth_client):
    """PROOF OF VULNERABILITY: GitHub callback with state= query param but NO
    oauth_state cookie should be rejected.

    On UNFIXED main: `if cookie_state and cookie_state != state` — when
    cookie_state is None/falsy, the check is SKIPPED entirely, allowing
    the OAuth code to be replayed by an attacker (CSRF bypass).

    Expected to FAIL on main (silently proceeds to code exchange with missing cookie).
    Expected to PASS after fix (returns 302 to state_mismatch error).

    Note: Since code exchange calls external GitHub, the test checks for
    the 302 error redirect before code exchange happens.
    """
    # No oauth_state cookie set. Pass a state param as attacker would.
    resp = auth_client.get(
        "/api/auth/github/callback",
        params={"code": "fake-code-from-attacker", "state": "attacker-state"},
        # Deliberately no cookies — missing oauth_state cookie
    )
    # Must redirect to error page (state_mismatch) when cookie is absent
    assert resp.status_code == 302, (
        f"Expected 302 redirect, got {resp.status_code}. "
        f"Unfixed code skips state check when cookie is missing (CSRF bypass)."
    )
    location = resp.headers.get("location", "")
    assert "state_mismatch" in location, (
        f"Expected state_mismatch in redirect location, got: {location!r}"
    )


def test_pov_google_callback_no_cookie_allows_bypass(auth_client):
    """PROOF OF VULNERABILITY: Google callback with state= query param but NO
    oauth_state cookie should be rejected.

    Same CSRF bypass as GitHub. Expected to FAIL on main; PASS after fix.
    """
    resp = auth_client.get(
        "/api/auth/google/callback",
        params={"code": "fake-code-from-attacker", "state": "attacker-state"},
        # Deliberately no cookies
    )
    assert resp.status_code == 302, (
        f"Expected 302 redirect, got {resp.status_code}. "
        f"Unfixed code skips state check when cookie is missing."
    )
    location = resp.headers.get("location", "")
    assert "state_mismatch" in location, (
        f"Expected state_mismatch in redirect location, got: {location!r}"
    )


# ── Green cases (verifying correct behavior after fix) ────────────────────────

def test_github_callback_mismatched_state_redirects_to_error(auth_client):
    """Cookie present but state value does not match → 302 state_mismatch.

    This was working even on main. Verifying it still works after fix.
    """
    resp = auth_client.get(
        "/api/auth/github/callback",
        params={"code": "some-code", "state": "actual-state-from-query"},
        cookies={"oauth_state": "different-state-in-cookie"},
    )
    assert resp.status_code == 302
    location = resp.headers.get("location", "")
    assert "state_mismatch" in location


def test_google_callback_mismatched_state_redirects_to_error(auth_client):
    """Cookie present but state value does not match → 302 state_mismatch."""
    resp = auth_client.get(
        "/api/auth/google/callback",
        params={"code": "some-code", "state": "actual-state-from-query"},
        cookies={"oauth_state": "different-state-in-cookie"},
    )
    assert resp.status_code == 302
    location = resp.headers.get("location", "")
    assert "state_mismatch" in location
