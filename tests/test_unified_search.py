"""Tests for GET /api/search — unified anonymous search across skills, loops,
bundles, and personalities.

feat/unified-search. Uses tests._app_factory.build_test_app so the route
registration is exercised the same way production wires it (Phase-0 lesson:
a router mounted only in a hand-rolled test FastAPI() can silently diverge
from what app.main.create_app actually serves).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.models import Bundle, Personality, Skill, Verifier
from tests._app_factory import build_test_app


# ── (a) route exists in the REAL create_app() route table ──────────────────


def test_search_route_registered_in_real_app():
    """Pins /api/search into the actual production app, not just the test shim."""
    from app.main import create_app

    app = create_app()
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/search" in paths


def test_search_prefix_is_public():
    """Pins the middleware allow-list entry so anonymous callers don't 401."""
    from app.middleware.api_key import APIKeyMiddleware

    prefixes = tuple(APIKeyMiddleware.PUBLIC_PREFIXES)
    assert any("/api/search".startswith(p) for p in prefixes), (
        "/api/search is not covered by any PUBLIC_PREFIXES entry — anonymous callers will 401."
    )


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def search_client(db_session, monkeypatch) -> TestClient:
    app = build_test_app(db_session=db_session, monkeypatch=monkeypatch)
    return TestClient(app, raise_server_exceptions=True)


def _seed_public_skill(db, slug="tdd-helper", title="TDD Helper", **kwargs) -> Skill:
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=title,
        description="Helps you run test-driven development loops.",
        category="devops",
        is_public=True,
        is_archived=False,
        **kwargs,
    )
    db.add(s)
    db.flush()
    return s


def _seed_private_skill(db, slug="secret-skill", title="TDD Secret", **kwargs) -> Skill:
    s = Skill(
        id=uuid4(),
        slug=slug,
        title=title,
        description="A private tdd-related skill that must never leak.",
        category="devops",
        is_public=False,
        is_archived=False,
        **kwargs,
    )
    db.add(s)
    db.flush()
    return s


def _seed_verifier(db, slug="tdd-loop", title="TDD Loop", is_public=True, is_archived=False) -> Verifier:
    v = Verifier(
        id=uuid4(),
        slug=slug,
        title=title,
        description="Runs until the test suite is green.",
        success_condition="all tests pass",
        verification_script="pytest -q",
        system_prompt="drive tests to green",
        max_turns=25,
        stopping_criteria={"success": "a", "failure": "b", "budget": "c"},
        tool_allowlist=["terminal"],
        is_public=is_public,
        is_archived=is_archived,
        run_count=3,
    )
    db.add(v)
    db.flush()
    return v


def _seed_bundle(db, slug="tdd-bundle", name="TDD Bundle", visibility="public") -> Bundle:
    b = Bundle(
        id=uuid4(),
        name=name,
        description="A bundle of TDD-flavored skills.",
        visibility=visibility,
        slug=slug,
    )
    db.add(b)
    db.flush()
    return b


def _seed_personality(
    db, slug="tdd-mentor", title="TDD Mentor", is_public=True, is_archived=False
) -> Personality:
    p = Personality(
        id=uuid4(),
        slug=slug,
        title=title,
        description="A ruthless TDD-driven mentor persona.",
        system_prompt="be a ruthless tdd mentor",
        is_public=is_public,
        is_archived=is_archived,
        install_count=1,
    )
    db.add(p)
    db.flush()
    return p


# ── (b) anonymous request returns 200 (pins public-prefix registration) ────


def test_anonymous_search_returns_200(search_client):
    res = search_client.get("/api/search", params={"q": "tdd"})
    assert res.status_code == 200, res.text


# ── (c) seeded public + private skill -> only public returned ──────────────


def test_only_public_skill_returned(search_client, db_session):
    _seed_public_skill(db_session)
    _seed_private_skill(db_session)

    res = search_client.get("/api/search", params={"q": "tdd"})
    assert res.status_code == 200
    slugs = [s["slug"] for s in res.json()["skills"]]
    assert "tdd-helper" in slugs
    assert "secret-skill" not in slugs


def test_only_public_verifier_returned(search_client, db_session):
    _seed_verifier(db_session, slug="tdd-public-loop", is_public=True)
    _seed_verifier(db_session, slug="tdd-private-loop", is_public=False)
    _seed_verifier(db_session, slug="tdd-archived-loop", is_public=True, is_archived=True)

    res = search_client.get("/api/search", params={"q": "tdd"})
    slugs = [x["slug"] for x in res.json()["loops"]]
    assert "tdd-public-loop" in slugs
    assert "tdd-private-loop" not in slugs
    assert "tdd-archived-loop" not in slugs


def test_only_public_bundle_returned(search_client, db_session):
    _seed_bundle(db_session, slug="tdd-public-bundle", visibility="public")
    _seed_bundle(db_session, slug="tdd-private-bundle", visibility="private")

    res = search_client.get("/api/search", params={"q": "tdd"})
    slugs = [x["slug"] for x in res.json()["bundles"]]
    assert "tdd-public-bundle" in slugs
    assert "tdd-private-bundle" not in slugs


def test_only_public_personality_returned(search_client, db_session):
    _seed_personality(db_session, slug="tdd-public-persona", is_public=True)
    _seed_personality(db_session, slug="tdd-private-persona", is_public=False)
    _seed_personality(db_session, slug="tdd-archived-persona", is_public=True, is_archived=True)

    res = search_client.get("/api/search", params={"q": "tdd"})
    slugs = [x["slug"] for x in res.json()["personalities"]]
    assert "tdd-public-persona" in slugs
    assert "tdd-private-persona" not in slugs
    assert "tdd-archived-persona" not in slugs


# ── (d) grouped shape contract: all four keys always present, lists ────────


def test_grouped_shape_contract_on_zero_results(search_client):
    """A query that matches nothing must still return the full shape with []
    lists, never null and never omitted keys."""
    res = search_client.get("/api/search", params={"q": "zzz-no-such-match-zzz"})
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) >= {"query", "skills", "loops", "bundles", "personalities"}
    for group in ("skills", "loops", "bundles", "personalities"):
        assert isinstance(body[group], list)
        assert body[group] == []
    assert body["query"] == "zzz-no-such-match-zzz"


def test_grouped_shape_contract_with_results(search_client, db_session):
    _seed_public_skill(db_session)
    _seed_verifier(db_session)
    _seed_bundle(db_session)
    _seed_personality(db_session)

    res = search_client.get("/api/search", params={"q": "tdd"})
    body = res.json()
    assert set(body.keys()) >= {"query", "skills", "loops", "bundles", "personalities"}
    for group in ("skills", "loops", "bundles", "personalities"):
        assert isinstance(body[group], list)
    assert len(body["skills"]) >= 1
    assert len(body["loops"]) >= 1
    assert len(body["bundles"]) >= 1
    assert len(body["personalities"]) >= 1

    # spot-check the per-type extras contract
    skill_card = body["skills"][0]
    assert {"slug", "title", "description", "category", "tier"} <= set(skill_card.keys())

    loop_card = body["loops"][0]
    assert {"slug", "title", "description", "max_turns", "tool_count", "run_count"} <= set(loop_card.keys())

    bundle_card = body["bundles"][0]
    assert {"slug", "name", "description", "skill_count"} <= set(bundle_card.keys())

    persona_card = body["personalities"][0]
    assert {"slug", "title", "description", "category", "tier"} <= set(persona_card.keys())


# ── (e) q shorter than 2 chars -> 422 (documented choice) ───────────────────


def test_short_query_returns_422(search_client):
    """q shorter than 2 chars is a 422 (FastAPI Query min_length=2), not an
    empty-result 200 — documented choice: a too-short query is a client error,
    distinct from a legitimate zero-result search."""
    res = search_client.get("/api/search", params={"q": "t"})
    assert res.status_code == 422

    res_empty = search_client.get("/api/search", params={"q": ""})
    assert res_empty.status_code == 422


def test_missing_query_returns_422(search_client):
    res = search_client.get("/api/search")
    assert res.status_code == 422


# ── (f) limit respected per group ───────────────────────────────────────────


def test_limit_respected_per_group(search_client, db_session):
    for i in range(8):
        _seed_public_skill(db_session, slug=f"tdd-skill-{i}", title=f"Tdd Skill {i}")

    res = search_client.get("/api/search", params={"q": "tdd", "limit": 3})
    assert res.status_code == 200
    assert len(res.json()["skills"]) == 3


def test_limit_clamped_to_max(search_client):
    res = search_client.get("/api/search", params={"q": "tdd", "limit": 999})
    assert res.status_code == 422  # le=20 constraint on the Query


def test_limit_default_is_five(search_client, db_session):
    for i in range(8):
        _seed_public_skill(db_session, slug=f"tdd-def-{i}", title=f"Tdd Default {i}")

    res = search_client.get("/api/search", params={"q": "tdd"})
    assert res.status_code == 200
    assert len(res.json()["skills"]) == 5
