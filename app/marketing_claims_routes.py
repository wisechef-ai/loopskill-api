"""claimgate server half (2026-10-07) — the marketing claims contract.

Serves the approved-facts contract that producer-side gates consume before
any marketing copy is generated or published:

    GET  /api/marketing/claims        -> approved facts + retired rules
    POST /api/marketing/claims/check  -> violations for a piece of draft copy

Consumer: ~/.hermes/scripts/marketing/claims_gate.py (WiseChef side), which
FAILS CLOSED — if these endpoints are down, no marketing copy is generated at
all. That is deliberate: on 2026-10-04 a hand-typed fact list shipped a
retired pricing ladder (Pro+ / cookbook allowances) to X. The contract is
derived LIVE from config/tiers.yaml (the same SSOT every other surface
reads), so a reprice or tier change propagates here with no second copy.

Contract shape (pinned by the consumer's wiring tests, test_claims_gate_wiring.py):
    GET /claims ->
        {"approved_facts": [str, ...],       # list, non-empty
         "retired_rules":  [dict, ...]}      # list (may be empty)
    POST /claims/check {"text": str} ->
        {"violations": [{"rule_id": str, "match": str, "reason": str,
                         "replacement": str | absent}]}

stdlib + fastapi only; no DB access — pure config derivation.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import APIRouter

router = APIRouter()

# config/tiers.yaml lives two levels up from app/ — same path discipline as
# app/tier_labels.py so both readers can never disagree about which file is
# the SSOT.
TIERS_YAML = Path(__file__).resolve().parent.parent / "config" / "tiers.yaml"

MAX_CHECK_TEXT = 20_000  # consumer truncates to this; mirror the cap here


def _load_tiers() -> dict:
    """Parse config/tiers.yaml. Raises on missing/corrupt — fail closed."""
    with open(TIERS_YAML) as f:
        data = yaml.safe_load(f)
    if not isinstance(data.get("tiers"), dict) or not data["tiers"]:
        raise ValueError("config/tiers.yaml has no tiers map")
    return data


@lru_cache(maxsize=1)
def _claims_contract() -> tuple[dict, tuple[dict, ...]]:
    """Build (contract_dict, retired_rules_tuple) from the tiers SSOT.

    lru_cached: the yaml only changes on deploy (it is in the deploy.yml
    path filter), so re-reading per request buys nothing and a hot path the
    copygen cron hammers should not re-parse YAML every call.

    Approved facts are DERIVED, never hand-typed: each public tier's name,
    price, bundle cap and key cap are read from the same yaml block the API
    enforces, so copy can only ever state what the code actually grants.
    """
    data = _load_tiers()
    tiers = data["tiers"]

    facts: list[str] = []
    rules: list[dict] = []

    for slug in sorted(tiers):
        cfg = tiers[slug] or {}
        name = cfg.get("display_name", slug)
        is_public = cfg.get("public", True) is not False

        price = cfg.get("price_usd")
        price_s = f"${price}/month" if price is not None else "price on request"
        bundles = cfg.get("bundle_limit", cfg.get("cookbook_limit"))
        keys = cfg.get("api_key_cap")

        keys_s = f"{keys} active API key{'s' if keys != 1 else ''}"
        if not is_public:
            # ah_1008: a non-public tier contributes a RULE, never a fact.
            # 0.9.61 appended its priced fact here too, so the public no-key
            # GET /claims published the hidden name+price while /claims/check
            # flagged that same sentence — the contract approved what its own
            # gate rejects. Even the "do not name it" warning named it.
            # Any mention of a non-public tier in marketing copy is a
            # violation. Match the display name and the db_slug.
            rules.append(
                {
                    "rule_id": f"tier-not-public-{slug}",
                    "patterns": [re.escape(name), re.escape(slug)],
                    "reason": f"{name} is retired from the public ladder — name a public tier instead",
                    "replacement": "Pro",
                }
            )
            continue
        facts.append(f"{name} tier: {price_s}, {bundles} private bundles, {keys_s}.")

    # Generic ladder discipline, stated WITHOUT naming any hidden tier, so the
    # instruction itself passes /claims/check (invariant pinned in tests).
    facts.append(
        "Only the tiers listed above are on the public pricing ladder. Never name, price or list allowances for any other tier."
    )

    # Product-name discipline (claimgate: the 10-04 post called the product
    # by a retired name and advertised retired allowances).
    rules.append(
        {
            "rule_id": "retired-product-name-recipes",
            "patterns": [r"\bRecipes\b(?!\s*API)"],
            "reason": "the product is LoopSkill — never call it Recipes",
            "replacement": "LoopSkill",
        }
    )
    rules.append(
        {
            "rule_id": "retired-term-cookbook",
            "patterns": [r"\bcookbooks?\b"],
            "reason": "cookbooks are bundles now — never say cookbook in marketing copy",
            "replacement": "bundles",
        }
    )
    # Fabricated-number guard: percentages and "N teams/hours" patterns in
    # generated copy are fabricated unless they came from the brief; the
    # gate cannot see the brief, so it blocks the shape.
    rules.append(
        {
            "rule_id": "no-fabricated-metrics",
            "patterns": [
                r"\b\d+\s*%\s*(faster|improvement|more|less|increase|decrease|saving)",
                r"\b\d+\s+(teams|users|agents)\s+(tested|switched|love)",
                r"\bsaved\s+\d+\s+hours\b",
            ],
            "reason": "unverifiable benchmark/social-proof figure — state it qualitatively or cite a real source",
            "replacement": "",
        }
    )

    contract = {
        "approved_facts": facts,
        "retired_rules": [r["rule_id"] for r in rules],
        "source": "config/tiers.yaml",
    }
    return contract, tuple(rules)


def _violations(text: str) -> list[dict]:
    """Check draft copy against the retired rules. Empty list = clean."""
    _, rules = _claims_contract()
    out: list[dict] = []
    for rule in rules:
        for pattern in rule["patterns"]:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if m:
                out.append(
                    {
                        "rule_id": rule["rule_id"],
                        "match": m.group(0),
                        "reason": rule["reason"],
                        "replacement": rule["replacement"],
                    }
                )
                break  # one violation per rule is enough for the rewrite loop
    return out


@router.get("/claims")
def get_claims() -> dict:
    """The claims contract: approved facts + retired rule ids.

    Consumer requires BOTH keys non-empty (approved_facts) / present
    (retired_rules) or it fails closed, which is the correct behavior when
    the SSOT itself is broken.
    """
    contract, _ = _claims_contract()
    return contract


@router.post("/claims/check")
def check_claims(body: dict) -> dict:
    """Violations in draft copy: {"text": "..."} -> {"violations": [...]}."""
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str):
        return {"violations": []}
    return {"violations": _violations(text[:MAX_CHECK_TEXT])}
