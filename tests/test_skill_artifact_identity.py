"""fix/skill-artifact-identity — GET /skill install-artifact identity regression guard.

Context: docs/recipes-skill/SKILL.md is the PRIMARY agent onboarding artifact
advertised on the LoopSkill homepage ("Install the loopskill skill from
app.loopskill.io/skill"). Before this fix it still onboarded agents to the
retired ``recipes`` brand (name: recipes, RECIPES_API_KEY,
recipes_fleet_* tool names, recipes.wisechef.ai/signin|library|pricing links).

This locks the served artifact so a future edit to docs/recipes-skill/*.md
cannot silently re-introduce the retired brand strings on the live install
route. Modeled on the app/mcp/ brand sweep in
``test_activate0701_d2_mcp_brand_sweep.py`` — same shape, narrower scope
(the /skill-served docs, not the whole MCP tool layer). A brain-dead
repo-wide ban is deliberately NOT added here: legitimate rename history
(CHANGELOG, tests, comments) and the separate live recipes.wisechef.ai
product both still legitimately mention the old brand.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.skill_serve_routes import SKILL_MD_PATH, _canonical_skill_md, skill_serve_router

_DOCS_DIR = SKILL_MD_PATH.parent  # docs/recipes-skill/

# QUICKSTART files mirrored by scripts/sync_recipes_skill.py alongside SKILL.md.
# Not served by skill_serve_routes.py directly, but they live in the same
# served-docs directory and are the artifacts a fresh install points users at
# next (README "Quickstarts" table links straight to them) — so the identity
# ban extends to them too (guard 4b).
_QUICKSTART_FILES = sorted(_DOCS_DIR.glob("QUICKSTART-*.md"))

# Strings that MUST appear in the served /skill artifact.
_REQUIRED = (
    "name: loopskill",
    "app.loopskill.io",
    "LOOPSKILL_API_KEY",
)

# Retired-brand strings that must NOT appear in the served /skill artifact.
_BANNED = (
    "name: recipes",
    "recipes.wisechef.ai/signin",
    "recipes.wisechef.ai/library",
    "recipes.wisechef.ai/pricing",
)


def _client() -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(skill_serve_router)
    return TestClient(app)


class TestServedSkillArtifactIdentity:
    """(a) GET /skill through the router — required strings present, banned strings absent."""

    @pytest.mark.parametrize("required", _REQUIRED)
    def test_served_body_contains_required_string(self, required: str):
        with _client() as client:
            body = client.get("/skill").text
        assert required in body, (
            f"GET /skill no longer serves the required LoopSkill identity string "
            f"{required!r} — the install artifact has drifted from the current brand."
        )

    @pytest.mark.parametrize("banned", _BANNED)
    def test_served_body_bans_retired_brand_string(self, banned: str):
        with _client() as client:
            body = client.get("/skill").text
        assert banned not in body, (
            f"GET /skill serves the retired-brand string {banned!r} — this is the "
            "primary agent onboarding artifact advertised on the LoopSkill homepage; "
            "it must not onboard agents to the retired recipes brand."
        )

    def test_canonical_skill_md_helper_matches_served_body(self):
        """Belt-and-suspenders: the cached loader used by the route is what we assert on."""
        with _client() as client:
            served = client.get("/skill").text
        assert served == _canonical_skill_md()


class TestQuickstartFilesIdentity:
    """(b) Extend the ban to QUICKSTART files living alongside the served SKILL.md."""

    def test_quickstart_files_exist(self):
        assert _QUICKSTART_FILES, (
            "expected QUICKSTART-*.md files in docs/recipes-skill/ — none found; "
            "if the docs moved, update _DOCS_DIR / SKILL_MD_PATH accordingly."
        )

    @pytest.mark.parametrize("path", _QUICKSTART_FILES, ids=lambda p: p.name)
    @pytest.mark.parametrize("banned", _BANNED)
    def test_quickstart_file_bans_retired_brand_string(self, path: Path, banned: str):
        text = path.read_text(encoding="utf-8")
        assert banned not in text, (
            f"{path.name} contains the retired-brand string {banned!r} — "
            "QUICKSTART files are mirrored alongside SKILL.md and linked from the "
            "served README; they must not carry the retired brand either."
        )

    @pytest.mark.parametrize("path", _QUICKSTART_FILES, ids=lambda p: p.name)
    def test_quickstart_file_references_current_brand(self, path: Path):
        text = path.read_text(encoding="utf-8")
        assert "loopskill" in text.lower() or "app.loopskill.io" in text, (
            f"{path.name} does not reference the current LoopSkill brand at all — "
            "check it wasn't missed during the rebrand."
        )


class TestSkillRouteWiredInRealApp:
    """(c) Pin against the REAL route table — the phase-0 lesson.

    A test-only router mount (as used above for body-content assertions) proves
    the route module works, but NOT that create_app() actually wires it in.
    Iterate the production app's real route table so a future refactor that
    forgets to include_router(skill_serve_router) in app.main trips CI instead
    of silently 404ing the homepage-advertised install phrase in prod.
    """

    def test_skill_route_exists_in_real_app_route_table(self):
        from app.main import create_app

        app = create_app()
        paths = {getattr(route, "path", None) for route in app.routes}
        for expected in ("/skill", "/skill/", "/SKILL.md"):
            assert expected in paths, (
                f"{expected} missing from the REAL create_app().routes route table — "
                "the homepage advertises 'Install the loopskill skill from "
                "app.loopskill.io/skill'; if this route table doesn't wire /skill, "
                "the advertised install command 404s in prod."
            )
