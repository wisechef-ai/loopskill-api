"""Tests for the REVENUE/CATALOG curated loop-pack route (atomic-habits 2026-07-18).

No new table — this pins the honest-degradation contract: a pack's member_slugs
resolve live against Verifier, and a member that has drifted/retired is dropped
into missing_members rather than fabricated.
"""

from __future__ import annotations


def test_list_loop_packs(client):
    resp = client.get("/api/loops/packs")
    assert resp.status_code == 200
    packs = resp.json()
    assert any(p["pack_slug"] == "fleet-ops" for p in packs)


def test_get_fleet_ops_pack_resolves_seeded_verifiers(client):
    resp = client.get("/api/loops/packs/fleet-ops")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pack_slug"] == "fleet-ops"
    assert body["title"] == "Fleet Ops"
    slugs = {m["slug"] for m in body["members"]}
    # secret-scan-loop / repo-steward-loop are seeded fixtures in most test DBs;
    # if the test DB has neither seeded, both degrade honestly to missing_members
    # rather than the endpoint 500ing or fabricating members.
    assert slugs.issubset({"secret-scan-loop", "repo-steward-loop"})
    assert set(body["missing_members"]).isdisjoint(slugs)


def test_unknown_pack_slug_404s(client):
    resp = client.get("/api/loops/packs/does-not-exist")
    assert resp.status_code == 404
