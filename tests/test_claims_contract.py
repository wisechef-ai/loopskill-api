"""claimgate_1006 — the claims contract and its check.

Incident: a scheduled X post (2026-10-04) advertised "Pro gets you 1 cookbook.
Pro+ gets you 20 cookbooks ... Tiers are free / pro / pro_plus" weeks after
pro_plus went public:false and cookbooks became bundles. These tests pin:

* REAL_INCIDENTS: real published text is caught, with the right rule ids.
  COMPOUNDING: every future false claim that ships gets a row here.
* CLEAN_CORPUS: the live pricing card and the contract's own approved facts
  pass. A contract that flags its own facts would be useless to producers.
* Derivation: retired tiers and allowed prices come from config/tiers.yaml,
  not from literals. Flipping a tier's public flag or repricing it changes
  the verdict with no other edit (the RED-proof that this is class-level).
* Portability: every pattern runs unchanged in the Postgres trigger.
* Routes: anonymous GET /api/marketing/claims and POST .../claims/check.
"""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from app.services import claims_contract as cc
from tests._app_factory import build_test_app

# (text, rule ids that MUST fire). Real published copy, trimmed to the claim.
REAL_INCIDENTS: list[tuple[str, set[str]]] = [
    (  # x.com/adkrawcz/status/2106658548880953759 — 2026-10-04
        "Recipes exists to close it. The tier math matters if you touch client work. "
        "Pro gets you 1 cookbook for your own setup. Pro+ gets you 20 cookbooks, each with "
        "its own API key, so client #1 through client #20 never share credentials. "
        "Tiers are free / pro / pro_plus.",
        {"brand-recipes-product", "cookbook-tier-allowance", "tier-not-public-pro_plus"},
    ),
    (  # x.com/adkrawcz/status/2078042238152540645 — 2026-07-17
        "$20/mo for Pro, zero recipes installed on your agent. Free tier gets you one cookbook. "
        "Pro is $20/mo for a single cookbook with its own API key. Pro Plus gives you 20 cookbooks.",
        {"price-not-on-ladder", "cookbook-tier-allowance", "tier-not-public-pro_plus"},
    ),
    (  # x.com/adkrawcz/status/2057023531842498747 — 2026-05-20
        "Recipes = the cookbook your AI agent fleet reads from. Pro+: 20 cookbooks, 20 API keys. "
        "https://recipes.wisechef.ai/docs?ref=x",
        {"tier-not-public-pro_plus", "cookbook-tier-allowance", "brand-recipes-domain"},
    ),
    (  # linkedin 2026-06-12
        "Recipes Pro+ solves this with cookbook handoff to up to 200 cookbooks in one call.",
        {"brand-recipes-product", "tier-not-public-pro_plus", "cookbook-tier-allowance"},
    ),
]

CLEAN_CORPUS = [
    # Live /pricing card (rendered 2026-10-06).
    "Private bundles: Free gives you 2, Pro gives you 50. That is the whole difference.",
    "Pro $9.95/month. 50 private bundles (Free gives you 2). Cancel anytime.",
    "Founding Member — $49, forever. Pay once, get Pro for life. Capped at 100 seats.",
    "Self-host $0 forever. Free $0 · forever. Hosted by us. No card.",
    "WiseChef installs, updates, and runs this exact skill catalog for you, from $199/month.",
    # Ordinary prose that shares words with retired rules.
    "We collected 50 skill recipes for chefs and a cookbook of prompts.",
    "Install the loopskill skill from app.loopskill.io/skill",
]


def _ids(text: str) -> set[str]:
    return {v["rule_id"] for v in cc.check_text(text)}


@pytest.mark.parametrize(("text", "expected"), REAL_INCIDENTS)
def test_real_incidents_are_caught(text: str, expected: set[str]) -> None:
    missing = expected - _ids(text)
    assert not missing, f"rules did not fire: {missing}"


@pytest.mark.parametrize("text", CLEAN_CORPUS)
def test_clean_corpus_passes(text: str) -> None:
    assert cc.check_text(text) == []


def test_approved_facts_pass_their_own_check() -> None:
    contract = cc.build_contract()
    assert contract["approved_facts"]
    for fact in contract["approved_facts"]:
        assert cc.check_text(fact, contract) == [], fact


def test_snapshot_prose_passes_the_contract(db_session) -> None:
    """The public marketing snapshot is subject to the same contract."""
    from app.marketing_routes import marketing_snapshot

    snap = marketing_snapshot(db_session)
    prose = []
    for tier in (snap.get("tiers") or {}).values():
        prose += [b for b in tier.get("bullets") or [] if isinstance(b, str)]
    prose += [pp["text"] for pp in snap.get("proof_points") or [] if isinstance(pp, dict) and "text" in pp]
    prose += [b for b in (snap.get("founding") or {}).get("bullets") or [] if isinstance(b, str)]
    assert prose
    for line in prose:
        assert cc.check_text(line) == [], line


def test_tier_number_binds_to_nearest_tier() -> None:
    assert _ids("Free gives you 2, Pro gives you 50 private bundles.") == set()
    assert _ids("Pro gives you 10 private bundles.") == {"tier-bundle-cap"}
    assert _ids("Free includes 3 API keys.") == {"tier-key-cap"}


def test_price_contexts() -> None:
    assert _ids("Pro is $20/mo") == {"price-not-on-ladder"}
    assert _ids("Pro at $12") == {"price-not-on-ladder"}
    assert _ids("€9,95 per month") == set()
    # A bare amount with no cadence and no tier word is not a price claim.
    assert _ids("We saved $3,000 in API spend.") == set()


# ── derivation: the contract follows tiers.yaml, not literals ────────────────


def _with_tiers(monkeypatch, tmp_path, mutate) -> None:
    doc = yaml.safe_load(cc.TIERS_YAML.read_text())
    mutate(doc)
    p = tmp_path / "tiers.yaml"
    p.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(cc, "TIERS_YAML", p)
    cc._cached_contract.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_cache():
    cc._cached_contract.cache_clear()
    yield
    cc._cached_contract.cache_clear()


def test_public_flag_drives_retirement(monkeypatch, tmp_path) -> None:
    assert "tier-not-public-pro_plus" in _ids("Pro+ is great")
    _with_tiers(monkeypatch, tmp_path, lambda d: d["tiers"]["pro_plus"].__setitem__("public", True))
    assert _ids("Pro+ is great") == set()
    assert 100.0 in cc.build_contract()["allowed_prices_usd"]


def test_reprice_retires_old_price(monkeypatch, tmp_path) -> None:
    assert _ids("Pro $9.95/month") == set()
    _with_tiers(monkeypatch, tmp_path, lambda d: d["tiers"]["pro"].__setitem__("price_usd", 12))
    assert _ids("Pro $9.95/month") == {"price-not-on-ladder"}
    assert any("$12/month" in f for f in cc.build_contract()["approved_facts"])


def test_cap_change_flows_into_facts(monkeypatch, tmp_path) -> None:
    _with_tiers(monkeypatch, tmp_path, lambda d: d["tiers"]["pro"].__setitem__("bundle_limit", 75))
    assert _ids("Pro gives you 50 private bundles.") == {"tier-bundle-cap"}
    assert any("Pro 75" in f for f in cc.build_contract()["approved_facts"])


def test_other_price_requires_evidence(monkeypatch, tmp_path) -> None:
    doc = yaml.safe_load(cc.CONTRACT_YAML.read_text())
    doc["other_prices"] = [{"amount_usd": 5, "product": "x"}]
    p = tmp_path / "claims_contract.yaml"
    p.write_text(yaml.safe_dump(doc))
    monkeypatch.setattr(cc, "CONTRACT_YAML", p)
    with pytest.raises(ValueError, match="evidence"):
        cc.build_contract()


# ── portability: the Postgres trigger runs the same patterns ─────────────────


def test_every_pattern_is_portable() -> None:
    c = cc.build_contract()
    for r in c["retired_rules"]:
        cc.assert_portable(r["pattern"])
        assert r["pg_pattern"] == cc.to_pg(r["pattern"])
        assert r"\b" not in r["pg_pattern"]
    for pr in c["price_rules"]:
        cc.assert_portable(pr["pattern"])


@pytest.mark.parametrize("bad", [r"(?<!x)Pro", r"(?i)pro", r"(a)\1", r"(?P<n>x)"])
def test_non_portable_patterns_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        cc.assert_portable(bad)


def test_contract_hash_is_stable_and_content_addressed() -> None:
    a = cc.build_contract()
    b = cc.build_contract()
    assert a["contract_hash"] == b["contract_hash"]
    body = {k: v for k, v in a.items() if k != "contract_hash"}
    import hashlib

    assert a["contract_hash"] == hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


# ── routes ───────────────────────────────────────────────────────────────────


def test_routes_anonymous(db_session, monkeypatch) -> None:
    client = TestClient(build_test_app(db_session=db_session, monkeypatch=monkeypatch))
    r = client.get("/api/marketing/claims")
    assert r.status_code == 200
    assert r.json()["retired_rules"]

    r = client.post("/api/marketing/claims/check", json={"text": REAL_INCIDENTS[0][0]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["approved_facts"], "a failing check must hand back the facts to rewrite from"

    r = client.post("/api/marketing/claims/check", json={"text": CLEAN_CORPUS[0]})
    assert r.json() == {
        "ok": True,
        "violations": [],
        "contract_hash": cc.build_contract()["contract_hash"],
        "approved_facts": [],
    }

    r = client.post("/api/marketing/claims/check", json={"text": "x" * (cc.MAX_CHECK_CHARS + 1)})
    assert r.status_code == 413
