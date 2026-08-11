"""bundles_0811 — the internal PATCH endpoint was unreachable since it shipped.

THE DEFECT
----------
`PATCH /api/internal/feedback/{row_id}/issue-url` authenticates with an
`X-Internal-Token` header. But `APIKeyMiddleware.dispatch` had no exemption for
`/api/internal/`, so it rejected the request with

    401 {"detail": "Invalid or missing x-api-key header"}

*before* `internal_routes._verify_token` ever ran. The endpoint could not be
called by anything, ever.

HOW IT SURFACED
---------------
The `feedback-dispatch.yml` workflow creates a GitHub issue and then PATCHes the
issue URL back so callers can poll for it. That final step silently failed for
every event type, so `federation_registry_proposals.issue_url` stayed NULL on
every row — a proposal would say "pending_review" with no way to reach the
review. It only became visible once `GITHUB_DISPATCH_PAT` was deployed and the
workflow got far enough to attempt the write-back.

Note the failure ORDER: the workflow reported success while doing nothing useful,
because issue creation happens first and the PATCH is best-effort. A green run is
not evidence that a step worked.

THE FIX AND WHY IT IS SAFE
--------------------------
Exempt `/api/internal/` from the x-api-key gate so the request reaches the route,
which authenticates it properly. This does NOT weaken auth — it routes to the
correct check. `_verify_token` fails CLOSED:

    expected = os.environ.get("INTERNAL_PATCH_TOKEN", "")
    if not expected or x_internal_token != expected: -> 403

so an unset, empty, or mismatched token is refused. Same pattern the
`/api/orgs/{id}/a2a-directory` bearer-token branch already uses.

These tests pin both halves: the middleware must let the request through, and
the route must still refuse anything without a valid token.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client_app(monkeypatch):
    """App with the internal router mounted and a known token."""
    monkeypatch.setenv("INTERNAL_PATCH_TOKEN", "test-internal-token-abc123")
    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


class TestMiddlewareLetsInternalRoutesReachTheirOwnAuth:
    def test_no_x_api_key_401_from_the_middleware(self, client_app):
        """The regression: middleware used to 401 before the route was reached.

        Any status EXCEPT the x-api-key 401 proves the request got past the
        middleware — 403 (bad token) and 404 (unknown row) are both fine here.
        """
        r = client_app.patch(
            f"/api/internal/feedback/{uuid4()}/issue-url",
            json={"issue_url": "https://github.com/x/y/issues/1", "table": "federation"},
            headers={"X-Internal-Token": "test-internal-token-abc123"},
        )
        assert r.status_code != 401, (
            "middleware is still rejecting /api/internal/ with the x-api-key gate — "
            f"body={r.text[:200]}"
        )
        assert "x-api-key" not in r.text.lower()


class TestTheRouteStillFailsClosed:
    """Exempting the path must not make the endpoint open."""

    def test_missing_token_is_forbidden(self, client_app):
        r = client_app.patch(
            f"/api/internal/feedback/{uuid4()}/issue-url",
            json={"issue_url": "https://github.com/x/y/issues/1", "table": "federation"},
        )
        assert r.status_code == 403, f"expected 403 with no token, got {r.status_code}"

    def test_wrong_token_is_forbidden(self, client_app):
        r = client_app.patch(
            f"/api/internal/feedback/{uuid4()}/issue-url",
            json={"issue_url": "https://github.com/x/y/issues/1", "table": "federation"},
            headers={"X-Internal-Token": "definitely-not-the-token"},
        )
        assert r.status_code == 403

    def test_unset_env_token_is_forbidden(self, monkeypatch):
        """An unconfigured deployment must refuse, never accept-anything."""
        monkeypatch.delenv("INTERNAL_PATCH_TOKEN", raising=False)
        from app.main import create_app

        c = TestClient(create_app(), raise_server_exceptions=False)
        r = c.patch(
            f"/api/internal/feedback/{uuid4()}/issue-url",
            json={"issue_url": "https://github.com/x/y/issues/1", "table": "federation"},
            headers={"X-Internal-Token": ""},
        )
        assert r.status_code == 403

    def test_empty_env_token_does_not_accept_empty_header(self, monkeypatch):
        """`if not expected` must short-circuit — otherwise '' == '' would pass."""
        monkeypatch.setenv("INTERNAL_PATCH_TOKEN", "")
        from app.main import create_app

        c = TestClient(create_app(), raise_server_exceptions=False)
        r = c.patch(
            f"/api/internal/feedback/{uuid4()}/issue-url",
            json={"issue_url": "https://github.com/x/y/issues/1", "table": "federation"},
            headers={"X-Internal-Token": ""},
        )
        assert r.status_code == 403


class TestExemptionIsNarrow:
    def test_other_api_paths_still_require_a_key(self, client_app):
        """The exemption must not leak to neighbouring routes."""
        r = client_app.get("/api/api-keys")
        assert r.status_code in (401, 403, 404, 405), (
            f"an x-api-key-gated route answered {r.status_code} — exemption too broad"
        )

    def test_the_guard_is_prefix_scoped_in_source(self):
        """Pin the exemption's shape so a later edit cannot widen it silently."""
        import inspect

        from app.middleware import api_key as m

        src = inspect.getsource(m.APIKeyMiddleware.dispatch)
        assert 'path.startswith("/api/internal/")' in src, (
            "the /api/internal/ exemption changed shape — verify it is still "
            "prefix-scoped and did not become a broad PUBLIC_PREFIXES entry"
        )
        assert os.sep is not None  # keep the import meaningful under lint
