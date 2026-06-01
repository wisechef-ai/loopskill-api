"""loopclose_3005 Phase B — canonical /skill serve route.

Proves GET /skill serves the in-repo SKILL.md directly as text/plain (200, no
302 to the GitHub mirror), with leak headers stripped and the correct MCP tool
list, so an agent can `curl -sL .../skill -o SKILL.md` and load it.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.skill_serve_routes import (
    SKILL_MD_PATH,
    _canonical_skill_md,
    _strip_leak_headers,
    skill_serve_router,
)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(skill_serve_router)
    return TestClient(app)


class TestSkillServeRoute:
    def test_returns_200_text_plain_no_redirect(self):
        with _client() as client:
            r = client.get("/skill", follow_redirects=False)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/plain")
        # No redirect — the whole point of Phase B.
        assert "location" not in {k.lower() for k in r.headers}

    def test_body_is_the_in_repo_source(self):
        with _client() as client:
            body = client.get("/skill").text
        # Served body equals the cleaned in-repo canonical source.
        assert body == _canonical_skill_md()
        assert body.startswith("---\nname: recipes")

    def test_no_leak_headers_in_served_body(self):
        with _client() as client:
            body = client.get("/skill").text
        for leak in ("auto-mirrored from", "DO NOT EDIT", "last sync: commit"):
            assert leak not in body, f"leak header leaked into /skill: {leak!r}"

    def test_no_phantom_cli(self):
        """The stale mirror named a nonexistent `recipes share <id>` CLI."""
        with _client() as client:
            body = client.get("/skill").text
        assert "recipes share <id>" not in body

    def test_correct_tool_count_and_tailor_tools_present(self):
        with _client() as client:
            body = client.get("/skill").text
        # Post-Phase-0 the canonical doc lists 26 tools incl. the tailor tools.
        assert "26 MCP tools available" in body
        assert "`recipes_tailor`" in body
        assert "`recipes_fork_list`" in body


class TestLeakHeaderStripping:
    def test_strips_mirror_bot_headers(self):
        dirty = (
            "<!-- auto-mirrored from wisechef-ai/recipes-api:docs/recipes-skill/SKILL.md -->\n"
            "<!-- DO NOT EDIT here — edit upstream and the bot will sync -->\n"
            "<!-- last sync: commit 2d0f8ad -->\n"
            "\n"
            "---\n"
            "name: recipes\n"
        )
        cleaned = _strip_leak_headers(dirty)
        assert cleaned.startswith("---\nname: recipes")
        assert "auto-mirrored" not in cleaned
        assert "DO NOT EDIT" not in cleaned
        assert "last sync" not in cleaned

    def test_clean_source_unchanged(self):
        clean = "---\nname: recipes\ndescription: x\n"
        assert _strip_leak_headers(clean) == clean

    def test_in_repo_source_is_already_clean(self):
        """The committed source must carry no leak headers (drift guard)."""
        raw = SKILL_MD_PATH.read_text(encoding="utf-8")
        for leak in ("auto-mirrored from", "DO NOT EDIT", "last sync: commit"):
            assert leak not in raw, f"in-repo SKILL.md contains a leak header: {leak!r}"
