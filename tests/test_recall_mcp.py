"""v7 Phase E — round-trip recall through the MCP tool dispatch.

Confirms ``recipes_recall`` is no longer a Phase-E stub and that the
service-layer wiring matches the contract documented in
:func:`app.recall_routes.recall_skills`.
"""

from __future__ import annotations

import json

from app.embeddings import embed_skill
from app.mcp.server import call_tool_sync
from app.mcp.tools import recipes_recall


def test_recall_no_longer_stub(db_session):
    out = recipes_recall(db_session, query="anything")
    # Stub returned {"error": "not_implemented", ...}; live impl returns hits.
    assert out.get("error") != "not_implemented"
    assert "hits" in out


def test_recall_via_mcp_dispatch(db_session):
    from tests.conftest import make_skill

    sk = make_skill(
        db_session,
        slug="csv-cleaner",
        title="CSV data cleaner",
        description="Cleans messy CSV files: dedupe, type coercion, header repair.",
        category="data",
        tier="free",
        related_skills=["data", "csv"],
    )
    sk.embedding = json.dumps(embed_skill(sk))
    db_session.flush()

    out = call_tool_sync(
        "recipes_recall",
        {"query": "clean a messy csv file", "limit": 3},
        db=db_session,
    )
    assert "hits" in out
    slugs = [h["slug"] for h in out["hits"]]
    assert "csv-cleaner" in slugs
    hit = next(h for h in out["hits"] if h["slug"] == "csv-cleaner")
    assert hit["install_status"] in ("available", "already_in_cookbook")
    assert hit["score"] > 0


def test_recall_empty_query_reports_error(db_session):
    out = call_tool_sync("recipes_recall", {}, db=db_session)
    assert out.get("error") == "query_required"
