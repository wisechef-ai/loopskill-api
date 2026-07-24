"""ah0724 rank-1 — surface agent_instructions + deploy_hint on the
composite-loop LIST serializer.

Live GET /api/composite-loops returned both flagship loops (atomic-habits,
dreaming) with no agent_instructions and no deploy affordance — browse cards
were dead while the DETAIL view (get_composite_loop) already carried
agent_instructions (ah0723 rank-1) + value_tagline (ah0724 rank-8). The
_composite_loop_agent_instructions helper was already synthesizing the text;
_composite_loop_to_out (the LIST serializer) just never called it. This adds
agent_instructions (str | None) and deploy_hint (bool) to CompositeLoopOut so
both LIST and DETAIL share the exact same field set, computed once per row.

Reuses the fixture helpers from test_ah0724_composite_loop_value_tagline.py
(same module, same DB-model shape) instead of duplicating them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_ah0724_composite_loop_value_tagline import (
    ATOMIC_HABITS_DESC,
    DREAMING_DESC,
    _mk_composite_loop,
)


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


@pytest.fixture
def flagship_loops(db_session):
    ah = _mk_composite_loop(
        db_session, slug="atomic-habits", title="Atomic Habits", description=ATOMIC_HABITS_DESC
    )
    dr = _mk_composite_loop(db_session, slug="dreaming", title="Dreaming", description=DREAMING_DESC)
    db_session.commit()
    return ah, dr


class TestAgentInstructionsOnListEndpoint:
    @pytest.mark.parametrize("slug", ["atomic-habits", "dreaming"])
    def test_agent_instructions_present_on_list(self, middleware_client, flagship_loops, slug):
        r = middleware_client.get("/api/composite-loops")
        assert r.status_code == 200, r.text
        rows = {row["slug"]: row for row in r.json()}
        assert slug in rows
        instructions = rows[slug]["agent_instructions"]
        assert instructions is not None
        assert instructions.strip() != ""
        # Same deploy-mechanics claim surface DETAIL already carries.
        assert "POST /api/composite-loops/" in instructions
        assert "GET /api/fleets" in instructions
        assert "sync tick (~30 min)" in instructions
        assert instructions.startswith("Why:")

    @pytest.mark.parametrize("slug", ["atomic-habits", "dreaming"])
    def test_deploy_hint_true_on_list(self, middleware_client, flagship_loops, slug):
        r = middleware_client.get("/api/composite-loops")
        assert r.status_code == 200, r.text
        rows = {row["slug"]: row for row in r.json()}
        assert rows[slug]["deploy_hint"] is True

    @pytest.mark.parametrize("slug", ["atomic-habits", "dreaming"])
    def test_list_and_detail_agent_instructions_match(self, middleware_client, flagship_loops, slug):
        """LIST and DETAIL must serve the byte-identical instructions string —
        no drift between the two call sites now that both compute it via the
        same _composite_loop_agent_instructions(cl) helper."""
        list_body = middleware_client.get("/api/composite-loops").json()
        list_row = next(row for row in list_body if row["slug"] == slug)
        detail_body = middleware_client.get(f"/api/composite-loops/{slug}").json()
        assert list_row["agent_instructions"] == detail_body["agent_instructions"]

    @pytest.mark.parametrize("slug", ["atomic-habits", "dreaming"])
    def test_detail_deploy_hint_true(self, middleware_client, flagship_loops, slug):
        r = middleware_client.get(f"/api/composite-loops/{slug}")
        assert r.status_code == 200, r.text
        assert r.json()["deploy_hint"] is True


class TestDeployHintFallback:
    def test_deploy_hint_false_when_no_instructions(self, db_session):
        """Unit-test: deploy_hint tracks whether agent_instructions is
        non-empty, not a hardcoded True. A loop whose helper somehow returns
        empty/None should report deploy_hint=False, not lie about deployability."""
        from app.composite_loop_routes import _composite_loop_to_out

        cl = _mk_composite_loop(
            db_session,
            slug="ah0724-deploy-hint-check",
            title="Deploy Hint Check",
            description="A loop used only to assert deploy_hint tracks agent_instructions.",
        )
        db_session.commit()
        out = _composite_loop_to_out(cl)
        # _composite_loop_agent_instructions always returns a non-empty
        # string for any valid CompositeLoop (it has a hardcoded mechanics
        # template with no early-return-None branch), so deploy_hint should
        # be True here — this test pins that invariant so a future change
        # that adds an empty-instructions branch doesn't silently ship
        # deploy_hint=True on a loop with no real instructions.
        assert out.deploy_hint == bool(out.agent_instructions)
        assert out.deploy_hint is True
