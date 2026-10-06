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
from pathlib import Path
import re

import pytest
import yaml
from fastapi.testclient import TestClient

from app.services import claims_contract as cc
from app.services import claims_normalize as cn
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
        {"price-recurring", "price-tier-pro", "cookbook-tier-allowance", "tier-not-public-pro_plus"},
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


# Adversarial review of PR #384 (gpt-6.1-sol, REQUEST_CHANGES): every bypass it
# found must now be caught, and every false positive it found must pass.
REVIEW_BYPASSES: list[tuple[str, set[str]]] = [
    ("LoopSkill costs 20 USD/mo", {"price-recurring-suffix"}),
    ("LoopSkill costs $20/m", {"price-recurring"}),
    ("Pro $199/month", {"price-tier-pro"}),
    ("Pro $49/month", {"price-tier-pro", "price-recurring"}),
    ("Founding Member $9.95 monthly", {"price-tier-founding"}),
    ("Pro at $9.95/month includes 20 private bundles", {"count-private-bundles", "tier-bundle-cap"}),
    ("Pro includes 50 private bundles and 20 API keys", {"count-api-keys", "tier-key-cap"}),
    ("Pro includes 20 private bundles", {"count-private-bundles", "tier-bundle-cap"}),
    ("Free gets two cookbooks", {"cookbook-tier-allowance"}),
    ("Pro gets 20  cookbooks", {"cookbook-tier-allowance"}),
    ("Recipes powers your agents", {"brand-recipes-product"}),
    ("Pro<b>+</b> for agencies", {"tier-not-public-pro_plus"}),
    ("Pro&#43; for agencies", {"tier-not-public-pro_plus"}),
    ("On-demand is $500", {"price-tier-contact-only"}),
]
# Round 3 (gpt-6.1-sol): shared bypasses in BOTH engines, now caught.
REVIEW_BYPASSES += [
    ("Pro&#0000000043; plan", {"tier-not-public-pro_plus"}),
    ("Pro&NonBreakingSpace;$199/month", {"unrecognised-html-entity"}),
    ("Pro\u200b+ for agencies", {"tier-not-public-pro_plus"}),
    ("Pro\u2003$199/month", {"price-tier-pro"}),
    ("Pro includes 1 API key", {"tier-key-cap"}),
    ("Pro&amp;plus; plan", {"unrecognised-html-entity"}),
]
# Round 4 (gpt-6.1-sol).
REVIEW_BYPASSES += [
    ("Pro includes 20 keys for API access", {"count-api-keys", "tier-key-cap"}),
    ("Pro&nbsp$199/month", {"price-tier-pro"}),
    ("Pro&#11141110; plan", {"unrecognised-html-entity"}),
    ("Pro&#x10FFFF0; plan", {"unrecognised-html-entity"}),
    ("Pro&#" + "9" * 5000 + "; plan", {"unrecognised-html-entity"}),
]
# Round 5 (gpt-6.1-sol).
REVIEW_BYPASSES += [
    ("Pro $9,950/month", {"price-tier-pro"}),
    ("Pro costs only $199/month", {"price-tier-pro", "price-recurring"}),
    ("Pro is only $199/month", {"price-tier-pro", "price-recurring"}),
    ("LoopSkill is $199/month", {"price-recurring"}),
    ("WiseChef: Pro costs $199/month", {"price-tier-pro"}),
]
# Round 6 (gpt-6.1-sol).
REVIEW_BYPASSES += [
    ("WiseChef costs $199.95/month", {"price-recurring"}),
    ("WiseChef costs $199 one-time", {"price-one-time"}),
    ("WiseChef integrates with LoopSkill which costs $199/month", {"price-recurring"}),
]
# Round 7 (gpt-6.1-sol).
REVIEW_BYPASSES += [
    ("WiseChef costs 1199 USD/month", {"price-recurring-suffix"}),
    ("LoopSkill costs, unlike WiseChef, $199/month.", {"price-recurring"}),
    ("WiseChef costs $199/month, and so does LoopSkill.", {"price-recurring"}),
]
# Round 8 (gpt-6.1-sol) + the thousands-separator gap found while fixing it.
REVIEW_BYPASSES += [
    ("WiseChef costs 1,199 USD/month", {"price-recurring-suffix"}),
    ("WiseChef costs 1 199 USD/month", {"price-recurring-suffix"}),
    ("WiseChef costs $1,199/month", {"price-recurring"}),
    ("LoopSkill costs $1,199/month", {"price-recurring"}),
    ("Pro is $1,995.00/month", {"price-recurring", "price-tier-pro"}),
]
# Round 9 (gpt-6.1-sol): whole count tokens; Windows-1252 numeric references.
REVIEW_BYPASSES += [
    ("Pro includes 1,050 private bundles", {"count-private-bundles", "tier-bundle-cap"}),
    ("Pro includes 1,010 API keys", {"count-api-keys", "tier-key-cap"}),
    ("Pro includes 1.050 private bundles", {"count-private-bundles", "tier-bundle-cap"}),
    ("Pro includes 1 050 private bundles", {"count-private-bundles", "tier-bundle-cap"}),
    ("Pro includes 1,2,50 private bundles", {"tier-bundle-cap"}),
    ("LoopSkill costs &#128;20/month", {"price-recurring"}),
    ("LoopSkill costs &#x80;20/month", {"price-recurring"}),
    ("LoopSkill costs 1,2,20 USD/month", {"price-recurring-suffix"}),
    ("Pro is $9.9.5/month", {"price-recurring", "price-tier-pro"}),
]
# Round 10 (gpt-6.1-sol): punctuation comma before a count; mixed grouping.
REVIEW_BYPASSES += [
    ("Pro includes 50 private bundles,20 API keys", {"count-api-keys", "tier-key-cap"}),
    ("LoopSkill costs $1,000.000.000/month", {"price-recurring"}),
    ("LoopSkill costs $1.000,000/month", {"price-recurring"}),
    ("Free: 2 private bundles,50 private bundles", {"tier-bundle-cap"}),
]
# Round 11 (gpt-6.1-sol): malformed space-separated runs.
MALFORMED_RUNS: list[tuple[str, set[str]]] = [
    ("Pro includes 1 50 private bundles", {"count-private-bundles", "tier-bundle-cap"}),
    ("LoopSkill costs $1 20/month", {"price-recurring"}),
    ("Free includes 1 0 API keys", {"count-api-keys", "tier-key-cap"}),
    ("LoopSkill costs 9 95 USD/month", {"price-recurring-suffix"}),
]
REVIEW_BYPASSES += MALFORMED_RUNS
# Round 12 (gpt-6.1-sol): block tags separate words.
BLOCK_TAG_CASES: list[tuple[str, set[str]]] = [
    ("Pro<br>includes 2 private bundles", {"tier-bundle-cap"}),
    ("Pro<br/>includes 2 private bundles", {"tier-bundle-cap"}),
    ("<p>Pro</p><p>includes 2 private bundles</p>", {"tier-bundle-cap"}),
    ("Recipes<br>powers your agents", {"brand-recipes-product"}),
    ("Recipes<BR >powers your agents", {"brand-recipes-product"}),
    ("Pro<b>+</b> for agencies", {"tier-not-public-pro_plus"}),
    # round 13: any tag may separate or join; quoted ">" inside attributes
    ("Pro<form>includes 2 private bundles</form>", {"tier-bundle-cap"}),
    ("Pro<custom-el>includes 2 private bundles", {"tier-bundle-cap"}),
    ('Pro<b title=">">+</b> for agencies', {"unsupported-markup"}),
    ("Pro<b title='>'>+</b> for agencies", {"unsupported-markup"}),
    # round 14: mixed inline + block tags
    ("P<b>ro</b><br>includes 2 private bundles", {"tier-bundle-cap"}),
    ("P<span>ro</span><p>includes 2 private bundles</p>", {"tier-bundle-cap"}),
    ("Pro<i>+</i><br>for agencies", {"tier-not-public-pro_plus"}),
    # round 15: Postiz's exact conversion (<p> breaks, <br> joins)
    ("P<br>ro<p>includes 2 private bundles</p>", {"tier-bundle-cap"}),
    ("Rec<br>ipes<p>powers your agents</p>", {"brand-recipes-product"}),
    ("P<br>ro<pre>includes 2 private bundles</pre>", {"tier-bundle-cap"}),
    # round 16: comments / quotes parsed exactly like striptags
    ("<p>P<!--'-->ro+ for agencies</p>", {"tier-not-public-pro_plus"}),
    ("P<!-- a -- b -->ro+ for agencies", {"tier-not-public-pro_plus"}),
    ('Pro<b title="\'>">+</b> for agencies', {"unsupported-markup"}),
]
REVIEW_BYPASSES += BLOCK_TAG_CASES
# Round 17 (claude-opus): Postiz runs parse5 BEFORE striptags; "<" that HTML5
# keeps as text must not hide the rest of the post.
REVIEW_BYPASSES += [
    ("We <3 you. Pro+ is $9/month", {"tier-not-public-pro_plus", "price-recurring"}),
    ("We <3 the Studio plan", {"legacy-tier-names"}),
    (
        "<p>We <3 LoopSkill. Pro+ is $100/month on the Cook plan.</p>",
        {"tier-not-public-pro_plus", "legacy-tier-names"},
    ),
    ("Plans <= Pro+ is $9/month", {"tier-not-public-pro_plus", "price-recurring"}),
    ("Pricing <\tPro+ is $9/month", {"tier-not-public-pro_plus", "price-recurring"}),
    ("<!-- x --!>Pro+ is $9/month<!-- -->", {"unsupported-markup"}),
    ('<?x "> Pro+ is $9/month <"?>', {"unsupported-markup"}),
]
# Round 18 (claude-opus): attribute values are published as text.
REVIEW_BYPASSES += [
    ('<p>Get it at <a href="https://recipes.wisechef.ai">our site</a></p>', {"brand-recipes-domain"}),
    ('<a href="https://wisechef.ai/recipes/pro">pricing</a>', {"brand-recipes-domain"}),
    ('<span data-mention-id="Pro+ for agencies">team</span>', {"unsupported-markup"}),
    # round 19: replaceBold joins the href to the neighbouring text
    ('<p>Get it at recipes<a href=".wisechef.ai">x</a></p>', {"brand-recipes-domain"}),
    ('<p>Pro is $1<a href="9">and</a>/mo</p>', {"price-tier-pro"}),
    ('<a class="x" href="https://app.loopskill.io">ok</a>', {"unsupported-markup"}),
]
REVIEW_MUST_PASS = [
    '<p>Plans: <a href="https://app.loopskill.io/pricing">current pricing</a></p>',
    "We <3 our users. LoopSkill is free to self-host.",
    "<p>Pro is $9.95/month.</p><br><p>Free includes 2 private bundles.</p>",
    "<p>Pro is $9.95/month.</p><p>Free includes 2 private bundles.</p>",
    "Free: 2 private bundles. Pro: 50 private bundles.",
    "Pro: 50 private bundles,10 API keys.",
    "Free: 2 private bundles, 1 API key.",
    "Pro is $9.95/month. Free includes 2 private bundles.",
    "Pro &#150; $9.95/month",
    "Pro is €9,95 per month.",
    "LoopSkill is free to self-host. WiseChef runs it for you from $199/month.",
    "WiseChef, the managed service, is $199 per month.",
    "Done-for-you: WiseChef runs it for you from $199/month.",
    "AT&T and R&D teams, Q&A after.",
    "We share 3 key lessons from shipping agents.",
    "R&D on agent skills, Q&A included.",
    "Free has 1 API key; Pro has 10 API keys.",
    "Pro saved $20 in API spend",
    "Free users can upgrade to Pro for 50 private bundles",
    "Founding Member: $49 one-time, Pro for life.",
]


def _ids(text: str) -> set[str]:
    return {v["rule_id"] for v in cc.check_text(text)}


@pytest.mark.parametrize(("text", "expected"), REVIEW_BYPASSES)
def test_review_bypasses_are_caught(text: str, expected: set[str]) -> None:
    missing = expected - _ids(text)
    assert not missing, f"rules did not fire: {missing} (got {_ids(text)})"


@pytest.mark.parametrize("text", REVIEW_MUST_PASS)
def test_review_false_positives_pass(text: str) -> None:
    assert cc.check_text(text) == []


def test_huge_numbers_cannot_crash_the_check() -> None:
    # 5000-digit token under the 20k cap: bounded patterns never convert it.
    cc.check_text("Free " + "1" * 5000 + " API keys and $" + "9" * 5000 + "/mo")


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
    assert _ids("Pro gives you 10 private bundles.") == {"count-private-bundles", "tier-bundle-cap"}
    assert _ids("Free includes 3 API keys.") == {"count-api-keys", "tier-key-cap"}
    # a cap that is real for ANOTHER tier: only the Python binding can see it
    assert _ids("Free includes 10 API keys.") == {"tier-key-cap"}


def test_price_contexts() -> None:
    assert _ids("Pro is $20/mo") == {"price-recurring", "price-tier-pro"}
    assert _ids("Pro at $12") == {"price-tier-pro"}
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
    assert {"price-recurring", "price-tier-pro"} <= _ids("Pro $9.95/month")
    assert any("$12/month" in f for f in cc.build_contract()["approved_facts"])


def test_cap_change_flows_into_facts(monkeypatch, tmp_path) -> None:
    _with_tiers(monkeypatch, tmp_path, lambda d: d["tiers"]["pro"].__setitem__("bundle_limit", 75))
    assert "tier-bundle-cap" in _ids("Pro gives you 50 private bundles.")
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
    for ar in c["amount_rules"]:
        cc.assert_portable(ar["pattern"])
        assert ar["pg_pattern"] == cc.to_pg(ar["pattern"])
        # the amount group really captures a number in Python
        assert re.compile(ar["pattern"]).groups >= ar["amount_group"]


@pytest.mark.parametrize(
    "bad", [r"(?<!x)Pro", r"(?i)pro", r"(a)\1", r"(?P<n>x)", r"\d+ keys", r"\s+Pro", r"\wPro"]
)
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

    r = client.post("/api/marketing/claims/check", json={"text": "Free " + "1" * 5000 + " API keys"})
    assert r.status_code == 200


def test_install_sql_patterns_match_python() -> None:
    """install.sql hard-codes the entity and unit patterns; they must be the
    Python ones, byte for byte (a stale copy shipped once in round 4)."""
    from pathlib import Path

    sql = (Path(__file__).resolve().parent.parent / "deploy" / "claimgate" / "install.sql").read_text()
    assert f"pat   constant text := '{cc._ENTITY.pattern}';" in sql
    assert f"unit_pat constant text := '{cc.to_pg(cc._UNIT.pattern)}';" in sql
    assert f"IF t ~ '{cc.THOUSANDS}' THEN" in sql
    assert "ARRAY['" + "', '".join(cc.TAG_READINGS) + "']" in sql
    for pattern in (cc.TAGLIKE, cc.POSTIZ_TAG, cc.BLOCK_TAG, cc.ALLOWED_TAG, cc.LINK):
        assert "'" + cc.to_pg(pattern).replace("'", "''") in sql, pattern
    assert f"IF t !~ '{cc.VALID_AMOUNT}' THEN" in sql
    assert f"lower(m[{cc.UNIT_GROUP}])" in sql
    assert f"claimgate.parse_amount(m[{cc.NUM_AFTER_GUARD}])" in sql
    c1 = sql.split("c1    constant text[] := ARRAY[", 1)[1].split("];", 1)[0]
    for cp, ch in cc.C1_REMAP.items():
        assert f"'{ch}'" in c1, hex(cp)


@pytest.mark.parametrize(
    ("token", "value"),
    [
        ("1,199", 1199.0),
        ("1,199.50", 1199.5),
        ("1.050,50", 1050.5),
        ("1 050", 1050.0),
        ("1 000,50", 1000.5),
        ("1.000.000", 1000000.0),
        ("9,95", 9.95),
        ("9.95", 9.95),
        ("49", 49.0),
    ],
)
def test_parse_amount(token: str, value: float) -> None:
    assert cc.parse_amount(token) == value


@pytest.mark.parametrize(
    "token", ["1,2,50", "9.9.5", "1,0500", "1 05", "1,000.000.000", "1.000,000", "1,000,00"]
)
def test_malformed_amount_is_nan(token: str) -> None:
    v = cc.parse_amount(token)
    assert v != v


def test_number_runs_stay_linear() -> None:
    """Worst cases for the run pattern (separator-led repetitions) on the
    20k-char API cap must stay fast: no catastrophic backtracking."""
    import time

    for body in ("1 " + "111 " * 4900, "1," * 9900, "$" + "1." * 9900, "1 111" * 3900):
        t0 = time.perf_counter()
        cc.check_text(body[: cc.MAX_CHECK_CHARS])
        assert time.perf_counter() - t0 < 2.0, body[:20]


PIPELINE_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "postiz_pipeline.json").read_text())


def _published_norm(published: str) -> str:
    """Postiz's published text through the gate's non-markup normalisation."""
    return re.sub(r"[ \t\r\n\f\v]+", " ", published.translate(cn._ZW_TABLE)).strip(" ")


@pytest.mark.parametrize(("html", "published"), PIPELINE_FIXTURE)
def test_join_reading_is_what_postiz_publishes(html: str, published: str) -> None:
    """For every input the gate does not reject as unsupported markup, the
    'join' reading IS the text Postiz publishes (stripHtmlValidation, real
    parse5 6.0.1 + striptags 3.2.0; fixture from gen_postiz_pipeline_fixture.js)."""
    if cc.unsupported_markup(html):
        assert "unsupported-markup" in {v["rule_id"] for v in cc.check_text(html)}
        return
    assert cc.normalize(html, "join") == _published_norm(published)


def test_production_markup_vocabulary_is_allowed() -> None:
    """Every tag found in the 447 production posts (2026-10-06): <p>, </p>, <br>."""
    assert cc.unsupported_markup("<p>a</p><br><br/><br /><p>b</p>") == []


def test_postiz_converter_lock_is_complete() -> None:
    """The pipeline fixture is only valid for the converter it was generated
    from; claimgate_sync.py alarms when the live one differs from this lock."""
    lock = json.loads(
        (Path(__file__).parent.parent / "deploy" / "claimgate" / "postiz_converter.lock.json").read_text()
    )
    assert len(lock["strip_js_sha256"]) == 64
    assert lock["parse5"] and lock["striptags"] and lock["container"] and lock["strip_js"]
