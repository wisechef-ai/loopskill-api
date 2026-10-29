"""coldstart_1029 — the no-key 401 must point a cold agent at the keyless
enroll funnel.

Observed live (run 20261029-85a16020 re-run, hermes/report-skill-error): a
cold agent with no key reached the correct write endpoint
(POST /api/v1/skill-error) and got `401 {"detail":"Invalid or missing
x-api-key header"}` — a terminal dead end, because no public surface it had
read connects "you need a key" to "you can mint one yourself, no human"
(POST /api/agents/register, documented in llms.txt Step 1). The agent gave
up honestly instead of fabricating a confirmation.

Contract pinned here: the middleware's no-key 401 keeps its exact `detail`
string (dozens of tests and docstrings assert it) and ADDS a
`how_to_get_a_key` object naming the enroll endpoint and its docs — so the
rejection is also a route forward. Public surfaces never see this body
(they short-circuit earlier); only auth-gated paths do.

Uses create_app() directly: the conftest ``client`` fixture builds a minimal
app that omits both APIKeyMiddleware and the skill-error router, so it can
never exercise this branch.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_no_key_401_keeps_detail_and_points_to_enroll():
    # No context manager: APIKeyMiddleware is ASGI-level and needs no
    # lifespan startup, and a second create_app() lifespan in one test
    # session trips DB-dependent startup hooks.
    c = TestClient(create_app())
    # POST /api/v1/skill-error is the exact surface tonight's cold agent
    # hit; the middleware rejects it before routing, so an empty body is
    # fine and no DB row is ever touched.
    r = c.post("/api/v1/skill-error", json={})
    assert r.status_code == 401
    body = r.json()
    # exact detail preserved — other tests, docs and mirrored strings assert it
    assert body["detail"] == "Invalid or missing x-api-key header"
    hint = body.get("how_to_get_a_key")
    assert isinstance(hint, dict), "no-key 401 must carry the enroll breadcrumb"
    assert hint["enroll"] == "POST /api/agents/register"
    assert hint["docs"] == "https://app.loopskill.io/llms.txt"
    assert hint["machine"] == "https://app.loopskill.io/.well-known/agent.json"


def test_public_surface_still_keyless():
    c = TestClient(create_app())
    # sanity counterpart: public health stays keyless — the breadcrumb
    # only appears where a key is genuinely required.
    r = c.get("/api/mcp/healthz")
    assert r.status_code == 200
