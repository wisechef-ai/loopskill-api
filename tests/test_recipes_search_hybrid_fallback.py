"""Hybrid search fallback tests — issue #111.

Verifies that ``/api/skills/search`` and the ``recipes_search`` MCP tool
fall through to recall-style ranking when the literal keyword pass returns
fewer than 3 hits AND a non-empty query was provided.

The reproducer from issue #106 / hermes-mac01:
    "development coding code review debugging testing github pull request
     refactor tdd planning software engineering"
returned 0 keyword hits but many recall hits. After this fix, search should
return ≥1 hit and flag ``hybrid_augmented=True``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.mcp.tools.search import recipes_search
from app.models import Skill


def _seed_dev_skills(db_session):
    """Seed the catalog with development-oriented skills that don't match the
    broad multi-keyword query *literally* (so the ILIKE pass returns 0) but DO
    match it semantically via BM25 over title+description+related_skills."""
    skills_data = [
        ("critical-code-reviewer", "Critical code reviewer",
         "Rigorous adversarial review of source code with zero tolerance for sloppiness."),
        ("gh-fix-ci", "GitHub CI fixer",
         "Diagnose and fix failing PR checks on GitHub Actions."),
        ("pr-draft", "Pull request drafter",
         "Generate structured PR descriptions from git diffs."),
        ("clean-code", "Clean code",
         "Write readable, maintainable code through disciplined naming."),
        ("clean-architecture", "Clean architecture",
         "Structure software around the Dependency Rule."),
        ("domain-driven-design", "Domain-driven design",
         "Model software around the business domain using bounded contexts."),
        ("gitnexus", "GitNexus codebase analysis",
         "Analyze codebases with GitNexus — index dependencies, call chains."),
    ]
    for slug, title, desc in skills_data:
        s = Skill(
            id=uuid4(),
            slug=slug,
            title=title,
            description=desc,
            category="dev-tools",
            tier="pro",
            is_public=True,
            is_archived=False,
            related_skills=[slug.replace("-", " ")],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db_session.add(s)
    db_session.flush()


def test_broad_dev_query_was_a_zero_hit_keyword_match(db_session):
    """Sanity check: the reproducer query genuinely has 0 literal matches.

    If this ever returns >0, the test below becomes meaningless — it would mean
    one of the seed titles/descriptions accidentally contains every word in the
    broad query, which is not a realistic agent search scenario.
    """
    _seed_dev_skills(db_session)
    broad = ("development coding code review debugging testing github pull "
             "request refactor tdd planning software engineering")
    # hybrid=False forces the legacy literal-only behaviour for the comparison.
    out = recipes_search(db_session, query=broad, hybrid=False)
    assert out["total"] == 0, (
        f"reproducer query no longer 0-hit on keyword path — got {out['total']}"
    )
    assert out["backend"] == "keyword"
    assert out["hybrid_augmented"] is False


def test_broad_dev_query_returns_hits_via_hybrid_fallback(db_session):
    """Issue #111: the same broad query should return results via hybrid."""
    _seed_dev_skills(db_session)
    broad = ("development coding code review debugging testing github pull "
             "request refactor tdd planning software engineering")
    out = recipes_search(db_session, query=broad, hybrid=True, limit=10)
    # With hybrid we should land at least 1 dev skill via BM25.
    assert out["total"] >= 1, f"hybrid fallback returned no results: {out}"
    assert out["hybrid_augmented"] is True
    assert out["backend"] in {"hybrid", "recall_only"}
    # Specifically, code-review oriented skills should rank.
    slugs = {r["slug"] for r in out["results"]}
    assert slugs & {"critical-code-reviewer", "pr-draft", "gh-fix-ci", "clean-code"}, (
        f"expected at least one dev skill in hybrid results, got: {slugs}"
    )


def test_specific_keyword_query_uses_keyword_path_only(db_session):
    """When the literal pass already returns >=3 hits, we don't widen."""
    _seed_dev_skills(db_session)
    # "code" appears in 4 titles/descriptions — well above the threshold.
    out = recipes_search(db_session, query="code", hybrid=True, limit=10)
    assert out["total"] >= 3
    assert out["hybrid_augmented"] is False
    assert out["backend"] == "keyword"


def test_hybrid_disabled_preserves_legacy_behaviour(db_session):
    """hybrid=False must give the exact old shape so existing callers don't
    silently start getting wider results without opt-in."""
    _seed_dev_skills(db_session)
    out = recipes_search(db_session, query="testing", hybrid=False)
    assert out["hybrid_augmented"] is False
    assert out["backend"] == "keyword"


def test_empty_query_does_not_invoke_hybrid(db_session):
    """No query → keyword path lists by recency. No hybrid widening."""
    _seed_dev_skills(db_session)
    out = recipes_search(db_session, query=None, hybrid=True, limit=10)
    assert out["backend"] == "keyword"
    assert out["hybrid_augmented"] is False


def test_hybrid_response_shape_is_stable(db_session):
    """Pin the keys so MCP clients can rely on the contract."""
    _seed_dev_skills(db_session)
    out = recipes_search(db_session, query="security audit codebase", hybrid=True)
    assert set(out.keys()) >= {"results", "total", "backend", "hybrid_augmented"}
    assert isinstance(out["results"], list)
    assert isinstance(out["total"], int)
    assert isinstance(out["backend"], str)
    assert isinstance(out["hybrid_augmented"], bool)
