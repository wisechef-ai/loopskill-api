"""Tests for SkillDetailOut.unhappy_paths_count (polish_1805 hotfix).

The field is a server-side SCALAR count of `unhappy_paths:` entries in the
skill's readme YAML frontmatter. It must:
  * count the `- condition:` items under the `unhappy_paths:` key
  * be present even for anonymous callers (it is not paywalled body content)
  * default to 0 when the readme is absent, has no frontmatter, or has no
    unhappy_paths block
  * stop counting at the next top-level YAML key (not bleed into siblings)
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.skill_routes import router as skill_router
from tests.conftest import make_skill

_THREE_PATHS = """---
name: demo-skill
description: A demo skill
unhappy_paths:
  - condition: network is down
    fix: retry with backoff
  - condition: disk full
    fix: free space
  - condition: auth expired
    fix: refresh token
tags:
  - condition: this is NOT an unhappy path, it is a tag
---

# Demo Skill

Body content here.
"""

_NO_BLOCK = """---
name: plain-skill
description: No unhappy paths declared
tags: [a, b]
---

# Plain Skill
"""


def _client(db_session) -> TestClient:
    app = FastAPI()
    app.include_router(skill_router, prefix="/api")

    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    return TestClient(app)


def test_counts_three_unhappy_paths(db_session):
    """Three `- condition:` entries → unhappy_paths_count == 3.

    The `tags:` block below also has a `- condition:` line; the count must
    stop at the next top-level key and NOT include it.
    """
    make_skill(db_session, slug="demo-skill", readme=_THREE_PATHS)
    db_session.commit()
    resp = _client(db_session).get("/api/skills/demo-skill")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "unhappy_paths_count" in body
    assert body["unhappy_paths_count"] == 3


def test_zero_when_no_unhappy_paths_block(db_session):
    """Readme with frontmatter but no `unhappy_paths:` key → count 0."""
    make_skill(db_session, slug="plain-skill", readme=_NO_BLOCK)
    db_session.commit()
    body = _client(db_session).get("/api/skills/plain-skill").json()
    assert body["unhappy_paths_count"] == 0


def test_zero_when_no_readme(db_session):
    """Skill with readme=None → count defaults to 0, no crash."""
    make_skill(db_session, slug="bare-skill", readme=None)
    db_session.commit()
    body = _client(db_session).get("/api/skills/bare-skill").json()
    assert body["unhappy_paths_count"] == 0


def test_count_present_for_anonymous_caller(db_session):
    """The count is a scalar, not paywalled body — anonymous caller sees it.

    The readme body itself stays paywalled (readme is None for an anon
    caller), but unhappy_paths_count must still be the real number.
    """
    make_skill(db_session, slug="demo-skill", readme=_THREE_PATHS)
    db_session.commit()
    body = _client(db_session).get("/api/skills/demo-skill").json()
    # anon caller: body is paywalled...
    assert body["readme"] is None
    # ...but the scalar count is still surfaced.
    assert body["unhappy_paths_count"] == 3
