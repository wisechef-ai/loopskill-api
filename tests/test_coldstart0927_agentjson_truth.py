"""coldstart_0927 — /.well-known/agent.json must tell the WHOLE truth.

Transcript evidence (run 20260927-df5287dd, codex / self-register-agent,
check_exit=1): the leg failed on a stack of two documentation defects, not on
the funnel itself —

1. ``registration`` documented every FAILURE status (400/401/409/429) but no
   SUCCESS status, while llms.txt (a separate repo, built from a snapshot of
   this document) claimed ``200``. The route answers ``201``. A strict cold
   client that hard-asserts the documented success status DISCARDED its
   shown-once ``api_key`` — the key is stored only as a hash, so that first
   mint is unrecoverable, and the re-registration burned 2 of the 3 allowed
   per-IP daily registrations.
2. The document advertised ``{origin}/openapi.json`` as the machine-readable
   API spec; that URL 404s live (the edge never proxies it — the same verified
   edge-routing fact ``test_gap_aiplugin_wellknown`` pins for ai-plugin.json).
   The failing leg fetched exactly that URL to resolve the status question
   and hit the dead link (ledger finding #5, open since 2026-09-06).

These tests pin the fixed contract:

* ``registration.success.status`` exists and equals the status the ROUTE
  DECORATOR actually declares — derived by introspection, not restated, so a
  future change to either side alone fails here;
* no advertised URL in the document points at the 404ing openapi.json path.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent_registration_routes import REGISTRATION_SUCCESS_STATUS
from tests._app_factory import build_test_app

AGENT_JSON_PATH = "/.well-known/agent.json"


@pytest.fixture()
def app(db_session, monkeypatch):
    return build_test_app(db_session=db_session, monkeypatch=monkeypatch)


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=False)


def _register_route_status(app) -> int | None:
    """The success status the register route's DECORATOR declares.

    Introspected from the live route object — the same source FastAPI itself
    uses when serialising the response — so this can never silently disagree
    with what the wire actually answers.
    """
    for route in app.routes:
        if getattr(route, "path", None) == "/api/agents/register":
            return getattr(route, "status_code", None)
    return None


class TestSuccessStatusIsPublished:
    """The defect class: a strict client hard-asserting the documented status
    threw away a shown-once key when the doc said 200 and the wire said 201."""

    def test_success_block_exists(self, client):
        reg = client.get(AGENT_JSON_PATH).json()["registration"]
        assert "success" in reg, "registration documents errors but no success contract"
        assert isinstance(reg["success"].get("status"), int)

    def test_published_success_status_matches_the_route_decorator(self, client, app):
        published = client.get(AGENT_JSON_PATH).json()["registration"]["success"]["status"]
        route_status = _register_route_status(app)
        assert route_status is not None, "register route not mounted"
        assert published == route_status, (
            f"agent.json says success is {published} but the route answers {route_status} "
            "— a strict client will discard its shown-once key over this"
        )

    def test_published_success_status_matches_the_shared_constant(self, client):
        published = client.get(AGENT_JSON_PATH).json()["registration"]["success"]["status"]
        assert published == REGISTRATION_SUCCESS_STATUS

    def test_success_description_warns_the_key_is_shown_once(self, client):
        """The cost of mis-reading the status is an unrecoverable key; the doc
        must say so next to the status it documents."""
        block = client.get(AGENT_JSON_PATH).json()["registration"]["success"]
        text = str(block.get("description", "")).lower()
        assert "once" in text


class TestNoDeadAdvertisedUrls:
    """Ledger finding #5: the discovery document advertised a URL that 404s
    live. The failing leg fetched it (to resolve the status question) and hit
    the dead link."""

    def test_agent_json_does_not_advertise_the_404ing_openapi_json(self, client):
        body = client.get(AGENT_JSON_PATH).text
        assert "openapi.json" not in body, (
            "agent.json advertises openapi.json — that path 404s live (edge never "
            "proxies it; see test_gap_aiplugin_wellknown for the verified fact)"
        )

    def test_registration_block_documents_no_success_status_other_than_the_route(
        self, client, app
    ):
        """Everything the registration block claims about statuses must match
        the route: error codes AND the success code (regression guard for the
        original llms.txt '200' drift, which lived one snapshot hop away)."""
        reg = client.get(AGENT_JSON_PATH).json()["registration"]
        documented_errors = {int(k) for k in reg["errors"]}
        route_status = _register_route_status(app)
        assert route_status not in documented_errors, (
            f"{route_status} is the SUCCESS status but is also listed under errors"
        )
        assert reg["success"]["status"] == route_status
