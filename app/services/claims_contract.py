"""Claims contract: what LoopSkill marketing may say, derived from the SSOTs.

Why this exists (2026-10-06): a scheduled X post went out on 2026-10-04
advertising "Pro gets you 1 cookbook. Pro+ gets you 20 cookbooks ... Tiers are
free / pro / pro_plus". Every one of those claims had been retired weeks
earlier: pro_plus is ``public: false`` in config/tiers.yaml, cookbooks became
bundles, Pro became 50 private bundles. The copy generator carried a
hand-typed "APPROVED FACTS" string frozen in June, and nothing between the
generator and the publish rail compared copy against tiers.yaml.

This module makes the comparison mechanical and keeps it in ONE place:

* :func:`build_contract` derives approved facts, the allowed price set and the
  retired-claim rules from ``config/tiers.yaml`` (prices, caps, public flag)
  plus ``config/claims_contract.yaml`` (brand renames, retired vocabulary).
  A tier flipped to ``public: false`` is retired everywhere on the next
  request; no marketing file needs editing.
* :func:`check_text` is the single implementation of the check. Producers
  call it over HTTP (``POST /api/marketing/claims/check``); the Postiz
  database trigger runs the same patterns (``pg_pattern``) synchronously.

Patterns are restricted to a regex subset that compiles identically under
Python ``re`` and PostgreSQL ARE; :func:`to_pg` is the only translation
(``\\b`` -> ``\\y``). The contract test enforces the subset.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG = Path(__file__).resolve().parent.parent.parent / "config"
TIERS_YAML = _CONFIG / "tiers.yaml"
CONTRACT_YAML = _CONFIG / "claims_contract.yaml"

MAX_CHECK_CHARS = 20_000

# A money amount and the two contexts that make it a price claim. Each entry
# is (pattern, amount_group): the Postgres trigger reads the same group index.
_AMOUNT = r"[$€] ?(\d+([.,]\d{1,2})?)"
_CADENCE = r" ?(/ ?mo|/ ?month|per month|a month|monthly|/ ?yr|/ ?year|per year|a year|one-time|forever)\b"
PRICE_PATTERNS: tuple[tuple[str, int], ...] = (
    # "$20/mo", "€100 per month", "$49 one-time"
    (_AMOUNT + _CADENCE, 1),
    # "Pro $20", "Pro at $20", "Founding Member: $49" — a tier word within a
    # short window before the amount, no sentence break and no other amount.
    (r"\b(Free|Pro|Founding|Enterprise|On-demand)\b[^.\n$€]{0,20}" + _AMOUNT, 2),
)

_PORTABLE_FORBIDDEN = re.compile(r"\(\?|\\[1-9]|\\[AZzGkpPN]|\(\?P")


def to_pg(pattern: str) -> str:
    """Translate a portable pattern to PostgreSQL ARE (word boundary only)."""
    return pattern.replace(r"\b", r"\y")


def assert_portable(pattern: str) -> None:
    """Raise ValueError if ``pattern`` uses syntax the Postgres trigger can't run."""
    if _PORTABLE_FORBIDDEN.search(pattern):
        raise ValueError(f"non-portable regex construct in {pattern!r}")
    re.compile(pattern, re.IGNORECASE)


def _num(value) -> str:
    """Render a price the way copy writes it: 9.95, 49, 0."""
    f = float(value)
    return str(int(f)) if f.is_integer() else f"{f:.2f}"


def _load_yaml(path: Path) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def _derived_rules(tiers: dict) -> list[dict]:
    """One retired rule per non-public tier: its display name, badge and slug."""
    rules = []
    for slug, cfg in tiers.items():
        if cfg.get("public", True):
            continue
        names = {slug}
        for key in ("display_name", "badge"):
            if cfg.get(key):
                names.add(str(cfg[key]))
        if str(cfg.get("display_name", "")).endswith("+"):
            names.add(str(cfg["display_name"])[:-1] + " Plus")
        alts = sorted({re.escape(n).replace(r"\ ", " ") for n in names}, key=len, reverse=True)
        rules.append(
            {
                "id": f"tier-not-public-{slug}",
                "pattern": r"\b(" + "|".join(alts) + r")",
                "reason": (
                    f"Tier {cfg.get('display_name', slug)!r} is public: false in config/tiers.yaml; "
                    "it is not on the public ladder and must not be advertised."
                ),
                "replacement": "the public tiers listed in this contract",
                "source": "config/tiers.yaml",
            }
        )
    return rules


def _facts(public: list[dict], founding: dict | None, static: list[str]) -> list[str]:
    ladder = ", ".join(
        f"{t['display_name']} ${_num(t['price_usd'])}" + ("/month" if float(t["price_usd"]) else "")
        for t in public
    )
    facts = [f"Public hosted tiers: {ladder}. Self-hosting is $0."]
    caps = [t for t in public if t.get("bundle_limit") is not None]
    if caps:
        facts.append(
            "Private bundles per tier: "
            + ", ".join(f"{t['display_name']} {t['bundle_limit']}" for t in caps)
            + ". Public bundles are unlimited on every tier."
        )
    keys = [t for t in public if t.get("api_key_cap") is not None]
    if keys:
        facts.append(
            "Active API keys per tier: "
            + ", ".join(f"{t['display_name']} {t['api_key_cap']}" for t in keys)
            + "."
        )
    if founding:
        facts.append(
            f"{founding['display_name']}: ${_num(founding['price_usd'])} one-time payment, "
            f"Pro for life, capped at {founding['slot_cap']} seats."
        )
    facts.extend(" ".join(str(s).split()) for s in static)
    return facts


@lru_cache(maxsize=1)
def _cached_contract(tiers_mtime: float, contract_mtime: float) -> str:
    tiers_doc = _load_yaml(TIERS_YAML)
    contract_doc = _load_yaml(CONTRACT_YAML)
    tiers = tiers_doc.get("tiers") or {}
    founding = tiers_doc.get("founding")

    public = [
        {
            "slug": slug,
            "display_name": cfg.get("display_name", slug.title()),
            "price_usd": cfg.get("price_usd"),
            "bundle_limit": cfg.get("bundle_limit", cfg.get("cookbook_limit")),
            "api_key_cap": cfg.get("api_key_cap"),
        }
        for slug, cfg in tiers.items()
        if cfg.get("public", True)
    ]
    allowed = {float(t["price_usd"]) for t in public if t["price_usd"] is not None}
    if founding and founding.get("price_usd") is not None:
        allowed.add(float(founding["price_usd"]))
    other_prices = contract_doc.get("other_prices") or []
    for op in other_prices:
        if not op.get("evidence"):
            raise ValueError(f"other_prices entry without evidence: {op!r}")
        allowed.add(float(op["amount_usd"]))

    rules = _derived_rules(tiers) + [
        {k: r.get(k) for k in ("id", "pattern", "reason", "replacement", "since")}
        | {"source": "config/claims_contract.yaml"}
        for r in contract_doc.get("rules") or []
    ]
    for r in rules:
        assert_portable(r["pattern"])
        r["pg_pattern"] = to_pg(r["pattern"])
        r["reason"] = " ".join(str(r.get("reason") or "").split())

    body = {
        "contract_version": 1,
        "public_tiers": public,
        "founding": (
            {
                "display_name": founding.get("display_name"),
                "price_usd": founding.get("price_usd"),
                "slot_cap": founding.get("slot_cap"),
            }
            if founding
            else None
        ),
        "allowed_prices_usd": sorted(allowed),
        "other_prices": other_prices,
        "approved_facts": _facts(public, founding, contract_doc.get("facts") or []),
        "retired_rules": rules,
        "price_rules": [{"pattern": p, "pg_pattern": to_pg(p), "amount_group": g} for p, g in PRICE_PATTERNS],
        "check_endpoint": "/api/marketing/claims/check",
    }
    body["contract_hash"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return json.dumps(body)


def build_contract() -> dict:
    """Return the claims contract. Cached until either yaml file changes."""
    return json.loads(_cached_contract(TIERS_YAML.stat().st_mtime, CONTRACT_YAML.stat().st_mtime))


def _excerpt(text: str, m: re.Match) -> str:
    lo, hi = max(0, m.start() - 30), min(len(text), m.end() + 30)
    return " ".join(text[lo:hi].split())


def check_text(text: str, contract: dict | None = None) -> list[dict]:
    """Return every claim violation in ``text`` (empty list = clean).

    Three checks, in the order a reader would notice them:
    1. retired rules (non-public tiers, brand renames, retired vocabulary);
    2. prices: every amount with a cadence or next to a tier name must be on
       the public ladder (``allowed_prices_usd``);
    3. tier numbers: "<tier> ... N private bundles / API keys" must equal that
       tier's live cap. The gap between tier name and number may not contain
       digits, so "Free gives you 2, Pro gives you 50 bundles" binds 50 to Pro.
    """
    c = contract or build_contract()
    text = text or ""
    out: list[dict] = []
    for rule in c["retired_rules"]:
        for m in re.finditer(rule["pattern"], text, re.IGNORECASE):
            out.append(
                {
                    "kind": "retired",
                    "rule_id": rule["id"],
                    "match": m.group(0),
                    "excerpt": _excerpt(text, m),
                    "reason": rule["reason"],
                    "replacement": rule.get("replacement"),
                }
            )
    allowed = {round(float(p), 2) for p in c["allowed_prices_usd"]}
    seen: set[tuple[int, int]] = set()
    for pat, group in PRICE_PATTERNS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            amount_text = m.group(group)
            span = m.span(group)
            if span in seen:
                continue
            seen.add(span)
            amount = round(float(amount_text.replace(",", ".")), 2)
            if amount not in allowed:
                out.append(
                    {
                        "kind": "price",
                        "rule_id": "price-not-on-ladder",
                        "match": m.group(0),
                        "excerpt": _excerpt(text, m),
                        "reason": f"{_num(amount)} is not a public price; allowed: "
                        + ", ".join(_num(p) for p in sorted(allowed)),
                        "replacement": None,
                    }
                )
    for tier in c["public_tiers"]:
        caps = {
            "bundle": tier.get("bundle_limit"),
            "key": tier.get("api_key_cap"),
        }
        pat = (
            r"\b"
            + re.escape(tier["display_name"])
            + r"\b[^.\n\d]{0,40}?\b(\d+) (private )?(bundles?|API keys?|keys?)\b"
        )
        for m in re.finditer(pat, text, re.IGNORECASE):
            n = int(m.group(1))
            unit = "key" if "key" in m.group(3).lower() else "bundle"
            if caps[unit] is not None and n != caps[unit]:
                out.append(
                    {
                        "kind": "tier-number",
                        "rule_id": f"tier-{unit}-cap",
                        "match": m.group(0),
                        "excerpt": _excerpt(text, m),
                        "reason": f"{tier['display_name']} {unit} cap is {caps[unit]}, copy says {n}",
                        "replacement": None,
                    }
                )
    return out
