"""claimgate v2 contract on GET /api/marketing/claims (ah_1011).

The Postiz-side rule sync on wisechef-hq (deploy/claimgate/claimgate_sync.py)
failed 800/800 runs: production served the v1 contract (retired_rules as
id strings, no amount_rules / public_tiers), and sync() refuses a contract
that looks pre-v2. So the Postgres gate ran on rules bootstrapped once from a
file, and a tiers.yaml reprice would never have reached it.

Pins:
  * GET /claims serves the v2 fields the sync needs, derived from
    config/tiers.yaml (+ config/claims_contract.yaml for the rule patterns);
  * the SERVED contract passes claimgate_sync.sync()'s acceptance rules
    (the real function, with psql captured instead of executed);
  * v1 consumers keep working: approved_facts unchanged, the v1 rule ids
    stay available as retired_rule_ids, /claims/check unchanged;
  * a tiers.yaml price change reaches the served amount rules (single SSOT);
  * contract_hash is identical across interpreter hash seeds (it changed
    per process before: same-length alternatives were ordered from a set).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import marketing_claims_routes as mcr  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services import claims_contract as cc  # noqa: E402

TIERS_YAML = ROOT / "config" / "tiers.yaml"
SYNC_PY = ROOT / "deploy" / "claimgate" / "claimgate_sync.py"


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app())


@pytest.fixture(scope="module")
def served() -> dict:
    # The handler itself (JSON round-tripped); one HTTP test below proves the
    # mount. Few anonymous requests: the per-IP rate-limit bucket is shared.
    return json.loads(json.dumps(mcr.get_claims()))


def test_route_serves_the_same_contract(client, served):
    r = client.get("/api/marketing/claims")
    assert r.status_code == 200
    assert r.json() == served


def _load_sync_module():
    spec = importlib.util.spec_from_file_location("claimgate_sync_under_test", SYNC_PY)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_served_contract_has_v2_fields(served):
    for key in (
        "contract_version",
        "public_tiers",
        "founding",
        "allowed_prices_usd",
        "amount_rules",
        "exempt_rules",
        "retired_rules",
        "contract_hash",
        "check_endpoint",
    ):
        assert key in served, key
    assert served["contract_version"] >= 4
    for r in served["retired_rules"]:
        assert isinstance(r, dict) and r["id"] and r["pg_pattern"] and "reason" in r
    for r in served["amount_rules"]:
        assert r["id"] and r["pg_pattern"] and isinstance(r["amount_group"], int) and r["allowed"] is not None
    for t in served["public_tiers"]:
        assert t["display_name"] and "bundle_limit" in t and "api_key_cap" in t


def test_served_contract_passes_claimgate_sync_acceptance(served, monkeypatch):
    """Run the REAL sync() against the served JSON; capture the SQL it would run."""
    sync = _load_sync_module()
    captured: list[str] = []
    monkeypatch.setattr(sync, "load_contract", lambda: json.loads(json.dumps(served)))
    monkeypatch.setattr(sync, "psql", lambda sql, check=True: captured.append(sql) or "")
    c = sync.sync()
    assert c["contract_hash"] == served["contract_hash"]
    sql = captured[-1]
    assert sql.startswith("BEGIN;") and sql.rstrip().endswith("COMMIT;")
    assert sql.count("'retired', ") == len(served["retired_rules"]) > 0
    assert sql.count("'amount', ") == len(served["amount_rules"]) > 0
    assert sql.count("INSERT INTO claimgate.tier ") == len(served["public_tiers"]) > 0


def test_public_tiers_and_prices_derive_from_tiers_yaml(served):
    tiers = yaml.safe_load(TIERS_YAML.read_text())["tiers"]
    public = {s: c for s, c in tiers.items() if c.get("public", True) is not False}
    assert {t["slug"] for t in served["public_tiers"]} == set(public)
    hidden = [c.get("display_name", s) for s, c in tiers.items() if c.get("public", True) is False]
    assert not {t["display_name"] for t in served["public_tiers"]} & set(hidden)
    for t in served["public_tiers"]:
        cfg = public[t["slug"]]
        assert t["price_usd"] == cfg.get("price_usd")
        assert t["bundle_limit"] == cfg.get("bundle_limit", cfg.get("cookbook_limit"))
        assert t["api_key_cap"] == cfg.get("api_key_cap")
        if cfg.get("price_usd") is not None:
            assert float(cfg["price_usd"]) in served["allowed_prices_usd"]


def test_tiers_yaml_reprice_reaches_amount_rules(tmp_path, monkeypatch):
    doc = yaml.safe_load(TIERS_YAML.read_text())
    doc["tiers"]["pro"]["price_usd"] = 12.34
    alt = tmp_path / "tiers.yaml"
    alt.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(cc, "TIERS_YAML", alt)
    cc._cached_contract.cache_clear()
    try:
        c = cc.build_contract()
        recurring = next(r for r in c["amount_rules"] if r["id"] == "price-recurring")
        assert 12.34 in recurring["allowed"] and 9.95 not in recurring["allowed"]
        assert {"slug": "pro", "price_usd": 12.34}.items() <= next(
            t for t in c["public_tiers"] if t["slug"] == "pro"
        ).items()
    finally:
        cc._cached_contract.cache_clear()


def test_v1_consumers_keep_working(served):
    v1, _ = mcr._claims_contract()
    # Chef's claims_gate.contract() needs both keys truthy; copygen reads approved_facts.
    assert served["approved_facts"] == v1["approved_facts"]
    assert served["retired_rules"]
    # The v1 /check rule ids stay available under their own key.
    assert served["retired_rule_ids"] == v1["retired_rules"]
    assert served["source"].startswith("config/tiers.yaml")
    res = mcr.check_claims({"text": "Pro+ gets you 20 cookbooks"})
    ids = {v["rule_id"] for v in res["violations"]}
    assert {"tier-not-public-pro_plus", "retired-term-cookbook"} <= ids
    assert set(res) == {"violations"}


def test_every_approved_fact_passes_both_engines(served):
    """#387 invariant, extended: the served facts pass /claims/check AND the
    v2 rules the Postgres trigger enforces (Python mirror of the same rules)."""
    for fact in served["approved_facts"]:
        assert mcr.check_claims({"text": fact})["violations"] == []
        assert cc.check_text(fact, cc.build_contract()) == [], fact


def test_served_hash_covers_the_served_body(served):
    import hashlib

    body = {k: v for k, v in served.items() if k != "contract_hash"}
    assert (
        served["contract_hash"] == hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    )


def test_contract_hash_is_stable_across_hash_seeds():
    snippet = "import sys; sys.path.insert(0, '.'); from app.services.claims_contract import build_contract; print(build_contract()['contract_hash'])"
    hashes = set()
    for seed in ("0", "1", "2", "3", "4", "5"):
        out = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=ROOT,
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        )
        hashes.add(out.stdout.strip())
    assert len(hashes) == 1, hashes


def _as_enforced(c: dict) -> dict:
    """The contract as claimgate_sync.sync() would leave claimgate.rule / .tier."""
    ex = {r["id"] for r in c["retired_rules"] + c["amount_rules"] if r.get("exemptable")}
    rules = [
        {"id": r["id"], "kind": "retired", "pg_pattern": r["pg_pattern"], "exemptable": r["id"] in ex}
        for r in c["retired_rules"]
    ]
    rules += [
        {
            "id": r["id"],
            "kind": "amount",
            "pg_pattern": r["pg_pattern"],
            "amount_group": r["amount_group"],
            "allowed": r["allowed"],
            "exemptable": r["id"] in ex,
        }
        for r in c["amount_rules"]
    ]
    rules += [
        {"id": r["id"], "kind": "exempt", "pg_pattern": r["pg_pattern"], "veto": r["pg_veto"]}
        for r in c["exempt_rules"]
    ]
    tiers = [
        {"name": t["display_name"], "bundle_cap": t["bundle_limit"], "key_cap": t["api_key_cap"]}
        for t in c["public_tiers"]
    ]
    return {"rules": rules, "tiers": tiers}


def test_strictness_diff_gate(served):
    """deploy/claimgate/rule_strictness_diff.py: run before every sync that replaces rules."""
    spec = importlib.util.spec_from_file_location(
        "rsd", ROOT / "deploy" / "claimgate" / "rule_strictness_diff.py"
    )
    rsd = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(rsd)  # type: ignore[union-attr]
    enforced = _as_enforced(served)
    assert rsd.diff(enforced, served)[0] == []

    weaker = json.loads(json.dumps(served))
    weaker["retired_rules"] = weaker["retired_rules"][1:]
    assert any(w.startswith("REMOVED") for w in rsd.diff(enforced, weaker)[0])

    weaker = json.loads(json.dumps(served))
    weaker["amount_rules"][0]["allowed"].append(999.0)
    assert any(w.startswith("ALLOWED MORE") for w in rsd.diff(enforced, weaker)[0])

    weaker = json.loads(json.dumps(served))
    weaker["public_tiers"][0]["bundle_limit"] = 10**6
    assert any(w.startswith("TIER") for w in rsd.diff(enforced, weaker)[0])

    weaker = json.loads(json.dumps(served))
    weaker["exempt_rules"].append({"id": "x", "pg_pattern": "x", "pg_veto": "y"})
    assert "NEW exempt rule x" in rsd.diff(enforced, weaker)[0]
