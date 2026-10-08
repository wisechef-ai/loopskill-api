"""claimgate server half (2026-10-07) — contract endpoints under /api/marketing.

Pins:
  * GET /api/marketing/claims serves derived approved facts + retired rules
    (never a hand-typed list) and both keys are present;
  * POST /api/marketing/claims/check flags a retired tier mention, retired
    product name, "cookbook", and fabricated-metric shapes;
  * clean copy passes with zero violations;
  * facts stay in lockstep with config/tiers.yaml (the SSOT the API itself
    enforces) — a reprice can never leave marketing copy stale.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import create_app  # noqa: E402

TIERS_YAML = Path(__file__).resolve().parent.parent / "config" / "tiers.yaml"


@pytest.fixture(scope="module")
def client():
    app = create_app()
    return TestClient(app)


def test_get_claims_contract_shape(client):
    r = client.get("/api/marketing/claims")
    assert r.status_code == 200
    body = r.json()
    # consumer's contract() fails closed unless both keys look like this
    assert isinstance(body.get("approved_facts"), list) and body["approved_facts"]
    assert isinstance(body.get("retired_rules"), list)


def test_approved_facts_derived_from_tiers_yaml(client):
    """Facts must match the SSOT the API enforces — no second hand-typed copy."""
    with open(TIERS_YAML) as f:
        tiers = yaml.safe_load(f)["tiers"]
    body = client.get("/api/marketing/claims").json()
    facts = "\n".join(body["approved_facts"])
    for slug, cfg in tiers.items():
        if cfg.get("public", True) is False:
            continue  # non-public tiers contribute a retired RULE only, never a fact (ah_1008)
        price = cfg.get("price_usd")
        if price is not None:
            assert f"${price}/month" in facts, f"{slug} price {price} missing from facts"


def test_every_approved_fact_passes_the_check(client):
    """ah_1008 invariant: the contract may never approve what its own gate rejects.

    0.9.61 served "Pro+ tier: $100/month, ..." as an approved fact while
    /claims/check flagged that exact sentence tier-not-public-pro_plus — a
    no-key public endpoint publishing a non-public price, and copygen fed a
    fact the gate then refuses.
    """
    facts = client.get("/api/marketing/claims").json()["approved_facts"]
    for fact in facts:
        v = client.post("/api/marketing/claims/check", json={"text": fact}).json()["violations"]
        assert v == [], f"approved fact fails the gate: {fact!r} -> {v}"


def test_non_public_tier_is_never_named_in_approved_facts(client):
    """No name, slug or price of a public:false tier may appear in the facts."""
    tiers = yaml.safe_load(open(TIERS_YAML))["tiers"]
    hidden = {s: c for s, c in tiers.items() if (c or {}).get("public", True) is False}
    assert hidden, "fixture premise: tiers.yaml carries at least one non-public tier"
    public_prices = {c.get("price_usd") for c in tiers.values() if (c or {}).get("public", True) is not False}
    facts = "\n".join(client.get("/api/marketing/claims").json()["approved_facts"])
    for slug, cfg in hidden.items():
        assert cfg.get("display_name", slug) not in facts, slug
        assert slug not in facts, slug
        price = cfg.get("price_usd")
        if price is not None and price not in public_prices:
            assert f"${price}/month" not in facts, f"{slug} price leaked"


def test_check_flags_non_public_tier_mention(client):
    pro_plus = yaml.safe_load(open(TIERS_YAML))["tiers"]["pro_plus"]["display_name"]
    r = client.post("/api/marketing/claims/check", json={"text": f"{pro_plus} gets you 200 bundles."})
    assert r.status_code == 200
    v = r.json()["violations"]
    assert v and any(x["rule_id"].startswith("tier-not-public") for x in v)
    for x in v:
        assert {"rule_id", "match", "reason"} <= set(x)


def test_check_flags_retired_product_name_and_cookbook(client):
    r = client.post("/api/marketing/claims/check", json={"text": "Recipes syncs your cookbooks everywhere."})
    ids = {x["rule_id"] for x in r.json()["violations"]}
    assert "retired-product-name-recipes" in ids
    assert "retired-term-cookbook" in ids


def test_check_flags_fabricated_metrics(client):
    for text in (
        "40% faster installs",
        "12 teams tested it",
        "saved 5 hours daily",
    ):
        v = client.post("/api/marketing/claims/check", json={"text": text}).json()["violations"]
        assert any(x["rule_id"] == "no-fabricated-metrics" for x in v), text


def test_check_clean_copy_passes(client):
    r = client.post(
        "/api/marketing/claims/check",
        json={"text": "Pro is $9.95/month with 50 private bundles. Every install is SHA256-verified."},
    )
    assert r.json()["violations"] == []


def test_check_handles_missing_or_oversize_text(client):
    assert client.post("/api/marketing/claims/check", json={}).json()["violations"] == []
    big = "x" * 25_000 + " Pro+ pro_plus cookbook"
    v = client.post("/api/marketing/claims/check", json={"text": big}).json()["violations"]
    assert v == []  # text truncated at 20k — the retired mentions are cut off
