"""ah0724 rank-8 REVENUE/CATALOG — converting copy on the composite-loop
catalog surface.

Both live composite loops (atomic-habits, dreaming) showed install_count=0
despite the deploy API + portal CTA shipping. This adds a `value_tagline`
field (computed at serve time, no DB migration) surfaced on both the LIST
endpoint (GET /api/composite-loops) and the DETAIL endpoint
(GET /api/composite-loops/{slug}), plus a value hook prepended to the
existing `agent_instructions` deploy-mechanics string.

Mirrors the migrated-sqlite-fixture-free pattern of test_composite_loop_deploy.py
(uses the shared `db_session` fixture from tests/conftest.py directly, no
alembic migration needed since these are pure serializer-layer additions).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def middleware_client(db_session, monkeypatch):
    from tests._app_factory import build_test_app

    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app)


def _mk_verifier(db, *, slug="ah0724-test-verifier"):
    from app.models import Verifier

    v = db.query(Verifier).filter(Verifier.slug == slug).first()
    if v is not None:
        return v
    v = Verifier(
        id=uuid.uuid4(),
        slug=slug,
        title="ah0724 Test Verifier",
        description="verifier for value_tagline tests",
        is_public=True,
        success_condition="the daily brief was written",
        verification_script="true",
        max_turns=25,
        stopping_criteria={"success": "done", "failure": "error", "budget": None},
        tool_allowlist=[],
        system_prompt="You are a verifier.",
    )
    db.add(v)
    db.flush()
    return v


def _mk_composite_loop(db, *, slug, title, description, prompt="do the thing"):
    from app.models import CompositeLoop

    verifier = _mk_verifier(db, slug=f"{slug}-verifier")
    cl = CompositeLoop(
        id=uuid.uuid4(),
        slug=slug,
        title=title,
        description=description,
        tier="free",
        is_public=True,
        schedule="24h",
        skills=[],
        connectors=[],
        subagents_config={},
        verifier_slug=verifier.slug,
        state_seed={},
        budget_usd=None,
        prompt=prompt,
    )
    db.add(cl)
    db.flush()
    return cl


ATOMIC_HABITS_DESC = (
    "A composite loop that observes the agent's recent work, identifies one "
    "1% improvement, ships it, and verifies it landed. Runs nightly."
)
DREAMING_DESC = (
    "A composite loop that runs during low-activity hours to consolidate "
    "daily memories, extract learnings, and prune stale ones. The agent's "
    "'sleep' cycle."
)


@pytest.fixture
def flagship_loops(db_session):
    ah = _mk_composite_loop(
        db_session, slug="atomic-habits", title="Atomic Habits", description=ATOMIC_HABITS_DESC
    )
    dr = _mk_composite_loop(db_session, slug="dreaming", title="Dreaming", description=DREAMING_DESC)
    db_session.commit()
    return ah, dr


class TestValueTaglineOnListEndpoint:
    @pytest.mark.parametrize("slug", ["atomic-habits", "dreaming"])
    def test_value_tagline_present_on_list(self, middleware_client, flagship_loops, slug):
        r = middleware_client.get("/api/composite-loops")
        assert r.status_code == 200, r.text
        rows = {row["slug"]: row for row in r.json()}
        assert slug in rows
        tagline = rows[slug]["value_tagline"]
        assert tagline is not None
        assert tagline.strip() != ""


class TestValueTaglineOnDetailEndpoint:
    @pytest.mark.parametrize("slug", ["atomic-habits", "dreaming"])
    def test_value_tagline_present_on_detail(self, middleware_client, flagship_loops, slug):
        r = middleware_client.get(f"/api/composite-loops/{slug}")
        assert r.status_code == 200, r.text
        body = r.json()
        tagline = body["value_tagline"]
        assert tagline is not None
        assert tagline.strip() != ""

    def test_atomic_habits_exact_tagline(self, middleware_client, flagship_loops):
        r = middleware_client.get("/api/composite-loops/atomic-habits")
        assert r.status_code == 200, r.text
        assert r.json()["value_tagline"] == (
            "Ships one verified 1% improvement to your agent every night — "
            "compounding gains while you sleep, no babysitting."
        )

    def test_dreaming_exact_tagline(self, middleware_client, flagship_loops):
        r = middleware_client.get("/api/composite-loops/dreaming")
        assert r.status_code == 200, r.text
        assert r.json()["value_tagline"] == (
            "Your agent's nightly sleep cycle: consolidates the day's "
            "memories, extracts what mattered, prunes the noise — so "
            "tomorrow it starts sharper."
        )


class TestAgentInstructionsValueHook:
    @pytest.mark.parametrize("slug", ["atomic-habits", "dreaming"])
    def test_agent_instructions_keeps_mechanics_and_gains_value_hook(
        self, middleware_client, flagship_loops, slug
    ):
        r = middleware_client.get(f"/api/composite-loops/{slug}")
        assert r.status_code == 200, r.text
        instructions = r.json()["agent_instructions"]
        # Mechanics claim surface — must still be present, byte-for-byte.
        assert "POST /api/composite-loops/" in instructions
        assert "GET /api/fleets" in instructions
        assert "sync tick (~30 min)" in instructions
        # New value hook.
        assert "Why:" in instructions
        assert instructions.startswith("Why:")


class TestValueTaglineFallback:
    def test_generic_fallback_uses_first_sentence_of_description(self, db_session):
        """Unit-test the helper directly for an unknown slug."""
        from app.composite_loop_routes import _composite_loop_value_tagline

        cl = _mk_composite_loop(
            db_session,
            slug="some-unbranded-loop-ah0724",
            title="Unbranded Loop",
            description="This loop does something useful. It has more detail here.",
        )
        db_session.commit()
        assert _composite_loop_value_tagline(cl) == "This loop does something useful."

    def test_fallback_returns_none_when_no_description(self, db_session):
        from app.composite_loop_routes import _composite_loop_value_tagline

        cl = _mk_composite_loop(
            db_session,
            slug="no-description-loop-ah0724",
            title="No Description",
            description=None,
        )
        db_session.commit()
        assert _composite_loop_value_tagline(cl) is None
