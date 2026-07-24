"""Tests for the full skill-graph dump endpoint (G17).

GET /api/skills/graph returns the entire derived edge set for portal-side
visualisation. No auth, single round-trip, weight-sorted.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.test_skill_derived_edges import make_skill_with_tags


@pytest.fixture
def graph_with_three_edges(db_session: Session):
    make_skill_with_tags(db_session, "a", ["x", "y", "z"], category="devops")
    make_skill_with_tags(db_session, "b", ["x", "y", "z"], category="devops")
    make_skill_with_tags(db_session, "c", ["x", "y"], category="devops")
    db_session.commit()
    from app.edge_builder import build_edges, persist_edges
    persist_edges(db_session, build_edges(db_session))
    db_session.commit()


class TestGraphDumpEndpoint:
    def test_returns_nodes_and_edges(self, client: TestClient, graph_with_three_edges):
        r = client.get("/api/skills/graph")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "nodes" in body
        assert "edges" in body
        assert isinstance(body["nodes"], list)
        assert isinstance(body["edges"], list)

    def test_nodes_carry_minimum_fields(self, client: TestClient, graph_with_three_edges):
        r = client.get("/api/skills/graph")
        body = r.json()
        for node in body["nodes"]:
            assert "slug" in node
            assert "title" in node
            assert "category" in node
            assert "tier" in node

    def test_edges_are_undirected_unique_pairs(self, client: TestClient, graph_with_three_edges):
        r = client.get("/api/skills/graph")
        body = r.json()
        seen = set()
        for e in body["edges"]:
            assert "source" in e
            assert "target" in e
            assert "weight" in e
            key = tuple(sorted([e["source"], e["target"]]))
            assert key not in seen, f"duplicate undirected pair {key}"
            seen.add(key)

    def test_edges_sorted_by_weight_desc(self, client: TestClient, graph_with_three_edges):
        r = client.get("/api/skills/graph")
        body = r.json()
        weights = [e["weight"] for e in body["edges"]]
        assert weights == sorted(weights, reverse=True)

    def test_only_public_skills_in_nodes(self, client: TestClient, db_session: Session):
        make_skill_with_tags(db_session, "pub", ["x", "y"], category="devops")
        make_skill_with_tags(db_session, "internal", ["x", "y"], category="devops",
                             is_public=False)
        db_session.commit()
        from app.edge_builder import build_edges, persist_edges
        persist_edges(db_session, build_edges(db_session))
        db_session.commit()
        r = client.get("/api/skills/graph")
        body = r.json()
        slugs = {n["slug"] for n in body["nodes"]}
        assert "internal" not in slugs

    def test_handles_empty_graph(self, client: TestClient):
        r = client.get("/api/skills/graph")
        assert r.status_code == 200
        body = r.json()
        assert body == {"nodes": [], "edges": [], "edge_count": 0, "node_count": 0}

    def test_includes_install_count_per_node(
        self, client: TestClient, graph_with_three_edges
    ):
        r = client.get("/api/skills/graph")
        body = r.json()
        for n in body["nodes"]:
            assert "install_count" in n
            assert isinstance(n["install_count"], int)

    def test_response_includes_meta_counts(self, client: TestClient, graph_with_three_edges):
        r = client.get("/api/skills/graph")
        body = r.json()
        assert body["node_count"] == len(body["nodes"])
        assert body["edge_count"] == len(body["edges"])
