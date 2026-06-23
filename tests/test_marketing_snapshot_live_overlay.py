"""Regression guard for drift_0604: marketing snapshot Class-B live overlay.

Before this fix, app/marketing_routes.py served mcp_tools / rest_endpoints /
price_usd as hand-copied static lists from config/recipes-marketing.yaml. They
rotted: the yaml advertised 6 MCP tools (4 of them phantom) while the live
registry had 30. This pins the contract that those fields are now derived from
their canonical machine SSOTs and can never drift again.

Contracts:
1. snapshot.mcp_tools == live registry roster (count + names), no phantoms.
2. snapshot.counts.mcp_tools_count == len(live roster).
3. rest_endpoints contains only paths that exist in the live app.
4. tier price_usd matches config/tiers.yaml (the Stripe-coupled SSOT).
5. proof_point text interpolates {mcp_tools_count} to the live count.
"""
from __future__ import annotations

from app.marketing_routes import (
    _live_mcp_tool_names,
    _live_rest_paths,
    marketing_snapshot,
)


def test_mcp_tools_match_live_registry(db_session) -> None:
    snap = marketing_snapshot(db_session)
    live = _live_mcp_tool_names()
    assert live, "registry should expose tools"
    assert snap["mcp_tools"] == live
    assert snap["counts"]["mcp_tools_count"] == len(live)


def test_no_phantom_tools_in_snapshot(db_session) -> None:
    # These 4 were the stale yaml entries that no longer exist in the registry.
    phantoms = {"recipes_detail", "recipes_trending", "recipes_stats", "recipes_install_meta_skill"}
    snap = marketing_snapshot(db_session)
    assert not (phantoms & set(snap["mcp_tools"])), "stale phantom tools must not resurface"


def test_rest_endpoints_all_live(db_session) -> None:
    live_paths = _live_rest_paths()
    assert live_paths, "app should expose routes"
    snap = marketing_snapshot(db_session)
    for ep in snap["rest_endpoints"]:
        assert ep in live_paths, f"advertised endpoint {ep} is not a live route"
    assert snap["counts"]["rest_endpoint_count"] == len(snap["rest_endpoints"])


def test_prices_track_tiers_yaml(db_session) -> None:
    from app.subscription_service import _load_tier_usd_price

    usd = _load_tier_usd_price()
    snap = marketing_snapshot(db_session)
    for slug, tier in snap["tiers"].items():
        if slug in usd:
            assert float(tier["price_usd"]) == float(usd[slug])


def test_proof_point_mcp_count_interpolated(db_session) -> None:
    snap = marketing_snapshot(db_session)
    count = snap["counts"]["mcp_tools_count"]
    texts = [pp["text"] for pp in snap.get("proof_points", []) if isinstance(pp, dict)]
    mcp_line = next((t for t in texts if "dedicated MCP tools" in t), None)
    assert mcp_line is not None
    assert f"{count} dedicated MCP tools" in mcp_line
    assert "{mcp_tools_count}" not in mcp_line  # placeholder must be resolved
