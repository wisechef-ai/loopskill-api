"""HTTP-level hybrid search tests — issue #111.

Covers the FastAPI route ``GET /api/skills/search?hybrid=...&q=...`` so the
contract is pinned at both the MCP and HTTP surfaces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.models import Skill


def _seed(db):
    for slug, title, desc in [
        ("pr-reviewer", "Pull request reviewer",
         "Reviews GitHub pull requests for bugs and style violations."),
        ("clean-code", "Clean code",
         "Disciplined naming and refactor patterns for maintainable software."),
        ("security-scanner", "Source code security scanner",
         "Scans a repository for XSS, SQLi, and secret leak patterns."),
    ]:
        db.add(Skill(
            id=uuid4(),
            slug=slug,
            title=title,
            description=desc,
            category="dev-tools",
            tier="cook",
            is_public=True,
            is_archived=False,
            related_skills=[],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        ))
    db.flush()


def test_search_returns_hybrid_keys_on_http(client, db_session):
    _seed(db_session)
    r = client.get("/api/skills/search?q=pull%20request")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "backend" in body
    assert "hybrid_augmented" in body
    # 'pull request' literally matches PR reviewer.
    assert body["total"] >= 1


def test_broad_query_triggers_hybrid_via_http(client, db_session):
    _seed(db_session)
    broad = ("development coding code review debugging testing github pull "
             "request refactor tdd planning software engineering")
    r = client.get(f"/api/skills/search?q={broad.replace(' ', '%20')}")
    assert r.status_code == 200, r.text
    body = r.json()
    # We seeded skills that BM25 should rank for this multi-keyword query.
    assert body["total"] >= 1, f"hybrid did not widen results: {body}"
    assert body["hybrid_augmented"] is True


def test_hybrid_false_param_disables_widening(client, db_session):
    _seed(db_session)
    broad = ("development coding code review debugging testing github pull "
             "request refactor tdd planning software engineering")
    r = client.get(
        f"/api/skills/search?q={broad.replace(' ', '%20')}&hybrid=false"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["hybrid_augmented"] is False
    assert body["backend"] == "keyword"
