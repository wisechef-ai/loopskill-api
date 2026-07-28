"""feat/loops-value-taglines — converting catalog copy on the /api/loops surface.

The /api/loops (and /api/verifiers) browse surface is the OLD registry — the
`Verifier` model, dual-mounted under both prefixes by app/loop_routes.py. The
composite-loop surface (/api/composite-loops) already shipped value_tagline +
agent_instructions + deploy_hint (PRs #135, #136), but the 10 starter loops on
/api/loops never got the equivalent — every browse card was dead copy.

This mirrors the composite-loop pattern EXACTLY but applied to Verifier:
  - serve-time computed fields on VerifierOut (no DB column, no migration),
  - per-slug bespoke copy for the 10 starter loops,
  - generic fallback (first sentence of description / success_condition) for
    any future user-published verifier.

Mirrors the fixture-free pattern of test_ah0724_composite_loop_value_tagline.py
(uses the shared `db_session` fixture from tests/conftest.py directly).
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


def _mk_verifier(
    db,
    *,
    slug="lvt-test-verifier",
    title="LVT Test Verifier",
    description: str | None = "a test verifier",
    success_condition="the thing was done",
):
    from app.models import Verifier

    v = db.query(Verifier).filter(Verifier.slug == slug).first()
    if v is not None:
        return v
    v = Verifier(
        id=uuid.uuid4(),
        slug=slug,
        title=title,
        description=description,
        is_public=True,
        success_condition=success_condition,
        verification_script="true",
        max_turns=25,
        stopping_criteria={"success": "done", "failure": "error", "budget": None},
        tool_allowlist=[],
        system_prompt="You are a verifier.",
    )
    db.add(v)
    db.flush()
    return v


# The 10 starter-loop slugs that must carry bespoke taglines on the live catalog.
STARTER_LOOP_SLUGS = [
    "repo-steward-loop",
    "pr-review-loop",
    "daily-briefing-loop",
    "test-green-loop",
    "lint-clean-loop",
    "hello-world-loop",
    "changelog-from-commits-loop",
    "doc-coverage-loop",
    "json-schema-validate-loop",
    "secret-scan-loop",
]


@pytest.fixture
def starter_loops(db_session):
    """Seed all 10 starter loops with realistic descriptions (mirrors live prod)."""
    descs = {
        "repo-steward-loop": "Wake up to a triaged repo: green Dependabot PRs merged.",
        "pr-review-loop": "Autonomous pull-request reviewer. Runs on every new PR.",
        "daily-briefing-loop": "Autonomous daily digest generator. Scrapes configured sources.",
        "test-green-loop": "Drive a change until the test suite is GREEN.",
        "lint-clean-loop": "Iterate until the linter reports zero violations.",
        "hello-world-loop": "The 30-second proof that a LoopSkill loop actually RUNS.",
        "changelog-from-commits-loop": "Produce a release CHANGELOG and prove it exists.",
        "doc-coverage-loop": "Drive a Python module to full public-docstring coverage.",
        "json-schema-validate-loop": "Drive a data file until it validates against a JSON Schema.",
        "secret-scan-loop": "Prove a working tree carries no obvious leaked credentials.",
    }
    for slug, desc in descs.items():
        _mk_verifier(db_session, slug=slug, title=slug, description=desc, success_condition="verified")
    db_session.commit()


class TestValueTaglineOnListEndpoint:
    @pytest.mark.parametrize("slug", STARTER_LOOP_SLUGS)
    def test_value_tagline_present_on_list(self, middleware_client, starter_loops, slug):
        r = middleware_client.get("/api/loops")
        assert r.status_code == 200, r.text
        rows = {row["slug"]: row for row in r.json()}
        assert slug in rows
        tagline = rows[slug]["value_tagline"]
        assert tagline is not None
        assert tagline.strip() != ""

    @pytest.mark.parametrize("slug", STARTER_LOOP_SLUGS)
    def test_value_tagline_within_12_words(self, middleware_client, starter_loops, slug):
        r = middleware_client.get("/api/loops")
        assert r.status_code == 200, r.text
        rows = {row["slug"]: row for row in r.json()}
        word_count = len(rows[slug]["value_tagline"].split())
        assert word_count <= 12, f"{slug} tagline is {word_count} words (max 12)"


class TestValueTaglineOnDetailEndpoint:
    @pytest.mark.parametrize("slug", STARTER_LOOP_SLUGS)
    def test_value_tagline_present_on_detail(self, middleware_client, starter_loops, slug):
        r = middleware_client.get(f"/api/loops/{slug}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["value_tagline"] is not None
        assert body["value_tagline"].strip() != ""

    def test_list_and_detail_tagline_identical(self, middleware_client, starter_loops):
        """The same helper feeds both surfaces — they must not drift."""
        list_resp = middleware_client.get("/api/loops")
        assert list_resp.status_code == 200
        list_rows = {row["slug"]: row for row in list_resp.json()}
        for slug in STARTER_LOOP_SLUGS:
            detail = middleware_client.get(f"/api/loops/{slug}")
            assert detail.status_code == 200
            assert list_rows[slug]["value_tagline"] == detail.json()["value_tagline"]


class TestAgentInstructions:
    @pytest.mark.parametrize("slug", STARTER_LOOP_SLUGS)
    def test_agent_instructions_present_on_list(self, middleware_client, starter_loops, slug):
        r = middleware_client.get("/api/loops")
        assert r.status_code == 200, r.text
        rows = {row["slug"]: row for row in r.json()}
        instr = rows[slug]["agent_instructions"]
        assert instr is not None
        assert len(instr) > 40  # real guidance, not a stub

    @pytest.mark.parametrize("slug", STARTER_LOOP_SLUGS)
    def test_agent_instructions_present_on_detail(self, middleware_client, starter_loops, slug):
        r = middleware_client.get(f"/api/loops/{slug}")
        assert r.status_code == 200, r.text
        assert r.json()["agent_instructions"] is not None

    def test_deploy_hint_tracks_agent_instructions(self, middleware_client, starter_loops):
        """deploy_hint must be True whenever agent_instructions is non-empty."""
        r = middleware_client.get("/api/loops")
        assert r.status_code == 200
        for row in r.json():
            if row["slug"] in STARTER_LOOP_SLUGS:
                assert row["deploy_hint"] is True
                assert bool(row["agent_instructions"]) == row["deploy_hint"]


class TestExactBespokeTaglines:
    """Pin the exact rendered string for each flagship loop so copy drift is caught."""

    EXPECTED = {
        "repo-steward-loop": ("Wake to a triaged repo: Dependabot merged, everything else commented."),
        "hello-world-loop": ("The 30-second proof a loop runs: passed=true, no setup."),
        "test-green-loop": ("Hand it red tests; it stops when the suite is green."),
    }

    @pytest.mark.parametrize("slug", list(EXPECTED))
    def test_exact_tagline(self, middleware_client, starter_loops, slug):
        r = middleware_client.get(f"/api/loops/{slug}")
        assert r.status_code == 200, r.text
        assert r.json()["value_tagline"] == self.EXPECTED[slug]


class TestFallbacks:
    def test_generic_tagline_fallback_uses_first_sentence(self, db_session):
        """Unknown slug with a description falls back to its first sentence."""
        from app.verifier_routes import _verifier_value_tagline

        v = _mk_verifier(
            db_session,
            slug="unknown-future-loop-lvt",
            description="This loop does something useful. It has more detail here.",
        )
        db_session.commit()
        assert _verifier_value_tagline(v) == "This loop does something useful."

    def test_generic_tagline_fallback_returns_none_when_no_description(self, db_session):
        from app.verifier_routes import _verifier_value_tagline

        v = _mk_verifier(db_session, slug="no-desc-loop-lvt", description=None)
        db_session.commit()
        assert _verifier_value_tagline(v) is None

    def test_generic_instructions_fallback_uses_success_condition(self, db_session):
        from app.verifier_routes import _verifier_agent_instructions

        v = _mk_verifier(
            db_session,
            slug="unknown-sc-loop-lvt",
            description="desc.",
            success_condition="the suite is green",
        )
        db_session.commit()
        instr = _verifier_agent_instructions(v)
        assert "the suite is green" in instr
        assert "POST /api/loops/unknown-sc-loop-lvt/run" in instr

    def test_generic_instructions_fallback_when_no_success_condition(self, db_session):
        """Fail-safe: returns a run hint rather than raising."""
        from app.verifier_routes import _verifier_agent_instructions

        # success_condition is NOT NULL on the model, so simulate via a stub.
        class StubV:
            slug = "edge-case-lvt"
            description = None
            success_condition = None

        instr = _verifier_agent_instructions(StubV())
        assert "POST /api/loops/edge-case-lvt/run" in instr


class TestCompatAliasSurfaceParity:
    """/api/loops and /api/verifiers bind to the SAME handlers (byte-identical)."""

    def test_value_tagline_identical_under_both_prefixes(self, middleware_client, starter_loops):
        list_loops = middleware_client.get("/api/loops")
        list_verifiers = middleware_client.get("/api/verifiers")
        assert list_loops.status_code == 200
        assert list_verifiers.status_code == 200
        loops_map = {r["slug"]: r["value_tagline"] for r in list_loops.json()}
        verifiers_map = {r["slug"]: r["value_tagline"] for r in list_verifiers.json()}
        for slug in STARTER_LOOP_SLUGS:
            assert loops_map[slug] == verifiers_map[slug]
