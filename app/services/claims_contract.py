"""Claims contract: what LoopSkill marketing may say, derived from the SSOTs.

Why this exists (2026-10-06): a scheduled X post went out on 2026-10-04
advertising "Pro gets you 1 cookbook. Pro+ gets you 20 cookbooks ... Tiers are
free / pro / pro_plus". Every one of those claims had been retired weeks
earlier: pro_plus is ``public: false`` in config/tiers.yaml, cookbooks became
bundles, Pro became 50 private bundles. The copy generator carried a
hand-typed "APPROVED FACTS" string frozen in June, and nothing between the
generator and the publish rail compared copy against tiers.yaml.

This module makes the comparison mechanical and keeps it in ONE place:

* :func:`build_contract` derives approved facts, retired-claim rules and
  amount rules (prices, bundle and key counts, each with its own allowed set)
  from ``config/tiers.yaml`` plus ``config/claims_contract.yaml``. A tier
  flipped to ``public: false`` or repriced changes the verdict on the next
  request; no marketing file needs editing.
* :func:`check_text` is the single implementation of the check. Producers
  call it over HTTP (``POST /api/marketing/claims/check``). The Postiz
  database trigger (deploy/claimgate/install.sql) runs the SAME retired and
  amount rules via ``pg_pattern``; tests/test_claims_contract_pg_parity.py
  asserts both engines agree on a shared corpus (postgres CI leg).
* The one check Postgres cannot express (binding a bundle/key count to the
  nearest preceding tier name) is Python-only; the trigger still enforces the
  coarse form (a count that is no tier's cap at all).

Portable regex subset (Python ``re`` and PostgreSQL ARE, en_US.utf8): literal
text, ``[...]`` classes, ``[0-9]`` (never ``\\d``: Unicode digits differ),
``\\b`` (translated to ``\\y``), groups, ``|``, ``?``, ``*``, ``+``,
``{m,n}``. No lookarounds, backreferences, named groups, inline flags or
``\\d \\w \\s`` shorthands. Both engines check the same normalised text
(:func:`normalize`; install.sql ``claimgate.normalize``).
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

# Amounts are bounded ({1,7}) so no input can force a huge int/float conversion.
_NUM = r"([0-9]{1,7}([.,][0-9]{1,2})?)"
_RECURRING = (
    r"(/ ?mo|/ ?m|/ ?month|per month|a month|monthly|/ ?yr|/ ?year|per year|a year|annually|yearly)\b"
)
_ONE_TIME = r"(one-time|one time|once|lifetime)\b"
# Only explicit connectors bind a price to a tier ("Pro is $X", "Pro at $X",
# "Pro: $X", "Pro plan for $X"), so "Pro saved $20 in API spend" is not a price.
_TIER_LINK = r"( plan| tier)?( is| costs| at| for| from| only)?:? ?[-–—]? ?"

_PORTABLE_FORBIDDEN = re.compile(r"\(\?|\\[1-9]|\\[AZzGkpPNdDwWsS]")


def to_pg(pattern: str) -> str:
    """Translate a portable pattern to PostgreSQL ARE (word boundary only)."""
    return pattern.replace(r"\b", r"\y")


def assert_portable(pattern: str) -> None:
    """Raise ValueError if ``pattern`` uses syntax the Postgres trigger can't run identically."""
    if _PORTABLE_FORBIDDEN.search(pattern):
        raise ValueError(f"non-portable regex construct in {pattern!r}")
    re.compile(pattern, re.IGNORECASE)


# Entity decoding is an explicit SPEC shared verbatim with install.sql
# (claimgate.decode_entities), not html.unescape: the two engines must agree
# byte for byte, and html.unescape's 2,200-name HTML5 table cannot be mirrored
# in SQL. Single pass, left to right: numeric (decimal / hex, ";" optional)
# and the named entities below (";" required). Anything else stays literal.
# Code points 0, U+D800-U+DFFF and > U+10FFFF stay literal.
NAMED_ENTITIES: dict[str, str] = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
    "nbsp": " ",
    "Tab": "\t",
    "NewLine": "\n",
    "plus": "+",
    "num": "#",
    "percnt": "%",
    "excl": "!",
    "quest": "?",
    "colon": ":",
    "semi": ";",
    "comma": ",",
    "period": ".",
    "sol": "/",
    "bsol": "\\",
    "lpar": "(",
    "rpar": ")",
    "ast": "*",
    "equals": "=",
    "lowbar": "_",
    "dollar": "$",
    "euro": "€",
    "pound": "£",
    "cent": "¢",
    "yen": "¥",
    "copy": "©",
    "reg": "®",
    "trade": "™",
    "times": "×",
    "divide": "÷",
    "middot": "·",
    "hellip": "…",
    "ndash": "–",
    "mdash": "—",
    "dash": "‐",
    "hyphen": "‐",
    "lsquo": "‘",
    "rsquo": "’",
    "ldquo": "“",
    "rdquo": "”",
    "shy": "",
    "ensp": " ",
    "emsp": " ",
    "thinsp": " ",
}
_ENTITY = re.compile(r"&(#[0-9]{1,7};?|#[xX][0-9a-fA-F]{1,6};?|[A-Za-z][A-Za-z0-9]{0,31};)")


def _decode_entity(m: re.Match) -> str:
    body = m.group(1).rstrip(";")
    if body.startswith("#"):
        cp = int(body[2:], 16) if body[1:2] in ("x", "X") else int(body[1:])
        if cp == 0 or 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
            return m.group(0)
        return chr(cp)
    return NAMED_ENTITIES.get(body, m.group(0))


def normalize(text: str) -> str:
    """The text BOTH engines check (install.sql: claimgate.normalize).

    1. tags removed WITHOUT inserting a space (``Pro<b>+</b>`` -> ``Pro+``);
    2. entities decoded once, per the explicit spec above;
    3. NBSP -> space; ASCII whitespace runs -> one space; trimmed.
    """
    text = re.sub(r"<[^>]+>", "", text or "")
    text = _ENTITY.sub(_decode_entity, text).replace("\u00a0", " ")
    return re.sub(r"[ \t\r\n\f\v]+", " ", text).strip()


def _num(value) -> str:
    """Render a number the way copy writes it: 9.95, 49, 0."""
    f = float(value)
    return str(int(f)) if f.is_integer() else f"{f:.2f}"


def _load_yaml(path: Path) -> dict:
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


def _derived_retired(tiers: dict) -> list[dict]:
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


def _tier_price_rules(rule_id: str, name_pattern: str, allowed: list[float], label: str) -> list[dict]:
    """Two rules per tier: "Pro is $X" (prefix currency) and "Pro costs X USD" (suffix)."""
    # A trailing \b after a name ending in punctuation ("Pro\+") could never
    # match before a space; only names ending in a word character get one.
    tail = r"\b" if re.search(r"[A-Za-z0-9_)?]$", name_pattern) else ""
    head = r"\b" + name_pattern + tail + _TIER_LINK
    # amount group = every capture group before the number, counted by the
    # regex engine itself (escaped literal parentheses do not count)
    before = re.compile(head).groups
    reason = f"price stated for {label} does not match its tier"
    return [
        {
            "id": rule_id,
            "pattern": head + r"[$€] ?" + _NUM,
            "amount_group": before + 1,
            "allowed": sorted(allowed),
            "reason": reason,
        },
        {
            "id": rule_id + "-suffix",
            "pattern": head + _NUM + r" ?(USD|EUR|dollars|euros|bucks)\b",
            "amount_group": before + 1,
            "allowed": sorted(allowed),
            "reason": reason,
        },
    ]


def _amount_rules(public: list[dict], founding: dict | None, other_prices: list[dict]) -> list[dict]:
    """Amount-bearing claims. Each rule names the ONLY amounts it may carry."""
    recurring = {float(t["price_usd"]) for t in public if t["price_usd"] is not None}
    recurring |= {float(o["amount_usd"]) for o in other_prices if o.get("cadence", "monthly") == "monthly"}
    one_time = {0.0}
    if founding and founding.get("price_usd") is not None:
        one_time.add(float(founding["price_usd"]))
    rules = [
        {
            "id": "price-recurring",
            "pattern": r"[$€] ?" + _NUM + " ?(USD|EUR)? ?" + _RECURRING,
            "amount_group": 1,
            "allowed": sorted(recurring),
            "reason": "recurring price not on the public ladder",
        },
        {
            "id": "price-recurring-suffix",
            "pattern": r"\b" + _NUM + r" ?(USD|EUR|dollars|euros|bucks) ?" + _RECURRING,
            "amount_group": 1,
            "allowed": sorted(recurring),
            "reason": "recurring price not on the public ladder",
        },
        {
            "id": "price-one-time",
            "pattern": r"[$€] ?" + _NUM + " ?(USD|EUR)?,? ?" + _ONE_TIME,
            "amount_group": 1,
            "allowed": sorted(one_time),
            "reason": "one-time price other than the Founding Member SKU",
        },
    ]
    for t in public:
        if t["price_usd"] is not None:
            rules += _tier_price_rules(
                f"price-tier-{t['slug']}",
                re.escape(t["display_name"]),
                [float(t["price_usd"])],
                t["display_name"],
            )
    if founding and founding.get("price_usd") is not None:
        rules += _tier_price_rules(
            "price-tier-founding", "Founding( Member)?", [float(founding["price_usd"])], "Founding Member"
        )
    rules += _tier_price_rules(
        "price-tier-contact-only", "(Enterprise|On-demand)", [], "On-demand (contact only)"
    )
    bundle_caps = {int(t["bundle_limit"]) for t in public if t.get("bundle_limit") is not None}
    key_caps = {int(t["api_key_cap"]) for t in public if t.get("api_key_cap") is not None}
    rules += [
        {
            "id": "count-private-bundles",
            "pattern": r"\b([0-9]{1,6}) private bundles?\b",
            "amount_group": 1,
            "allowed": sorted(bundle_caps),
            "reason": "private-bundle count is no tier's cap",
        },
        {
            "id": "count-api-keys",
            "pattern": r"\b([0-9]{1,6}) (active |scoped |separate )?(API )?keys\b",
            "amount_group": 1,
            "allowed": sorted(key_caps),
            "reason": "API-key count is no tier's cap",
        },
    ]
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
    other_prices = contract_doc.get("other_prices") or []
    for op in other_prices:
        if not op.get("evidence"):
            raise ValueError(f"other_prices entry without evidence: {op!r}")

    retired = _derived_retired(tiers) + [
        {k: r.get(k) for k in ("id", "pattern", "reason", "replacement", "since")}
        | {"source": "config/claims_contract.yaml"}
        for r in contract_doc.get("rules") or []
    ]
    amounts = _amount_rules(public, founding, other_prices)
    for r in retired + amounts:
        assert_portable(r["pattern"])
        r["pg_pattern"] = to_pg(r["pattern"])
        r["reason"] = " ".join(str(r.get("reason") or "").split())

    body = {
        "contract_version": 2,
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
        "allowed_prices_usd": sorted(
            {p for r in amounts if r["id"].startswith("price-") for p in r["allowed"]}
        ),
        "other_prices": other_prices,
        "approved_facts": _facts(public, founding, contract_doc.get("facts") or []),
        "retired_rules": retired,
        "amount_rules": amounts,
        "check_endpoint": "/api/marketing/claims/check",
    }
    body["contract_hash"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return json.dumps(body)


def build_contract() -> dict:
    """Return the claims contract. Cached until either yaml file changes."""
    return json.loads(_cached_contract(TIERS_YAML.stat().st_mtime, CONTRACT_YAML.stat().st_mtime))


def _violation(kind: str, rule_id: str, m: re.Match, text: str, reason: str, replacement=None) -> dict:
    lo, hi = max(0, m.start() - 30), min(len(text), m.end() + 30)
    return {
        "kind": kind,
        "rule_id": rule_id,
        "match": m.group(0),
        "excerpt": text[lo:hi],
        "reason": reason,
        "replacement": replacement,
    }


def check_text(text: str, contract: dict | None = None) -> list[dict]:
    """Return every claim violation in ``text`` (empty list = clean).

    1. retired rules: non-public tiers, brand renames, retired vocabulary;
    2. amount rules: every price / private-bundle count / API-key count must
       be one of the amounts its rule allows (the trigger runs 1 and 2 with
       identical patterns on identically normalised text);
    3. tier binding (Python only): a bundle/key count is bound to the NEAREST
       preceding tier name in the same sentence and must equal that tier's
       cap. "Free users can upgrade to Pro for 50 private bundles" binds 50 to
       Pro; "Pro includes 50 private bundles and 20 API keys" flags the 20.
    """
    c = contract or build_contract()
    text = normalize(text)
    out: list[dict] = []
    for rule in c["retired_rules"]:
        for m in re.finditer(rule["pattern"], text, re.IGNORECASE):
            out.append(_violation("retired", rule["id"], m, text, rule["reason"], rule.get("replacement")))
    for rule in c["amount_rules"]:
        allowed = {round(float(a), 2) for a in rule["allowed"]}
        for m in re.finditer(rule["pattern"], text, re.IGNORECASE):
            amount = round(float(m.group(rule["amount_group"]).replace(",", ".")), 2)
            if amount not in allowed:
                shown = ", ".join(_num(a) for a in sorted(allowed)) or "none (contact only)"
                out.append(
                    _violation(
                        "amount", rule["id"], m, text, f"{rule['reason']}: {_num(amount)} (allowed: {shown})"
                    )
                )
    out += _tier_binding(text, c)
    return out


_SENTENCE_END = re.compile(r"[.!?] ")
_UNIT = re.compile(
    r"\b([0-9]{1,6}) (private bundles?|(active |scoped |separate )?(API )?keys)\b", re.IGNORECASE
)


def _tier_binding(text: str, c: dict) -> list[dict]:
    tiers = {t["display_name"].lower(): t for t in c["public_tiers"]}
    if not tiers:
        return []
    name_re = re.compile(r"\b(" + "|".join(re.escape(n) for n in tiers) + r")\b", re.IGNORECASE)
    out = []
    for m in _UNIT.finditer(text):
        # sentence start = after the last ".", "!" or "?" FOLLOWED BY a space
        # ("$9.95" is not a sentence end)
        ends = [e.end() for e in _SENTENCE_END.finditer(text, 0, m.start())]
        start = ends[-1] if ends else 0
        names = list(name_re.finditer(text, start, m.start()))
        if not names:
            continue
        tier = tiers[names[-1].group(1).lower()]
        unit = "key" if "key" in m.group(2).lower() else "bundle"
        cap = tier.get("api_key_cap") if unit == "key" else tier.get("bundle_limit")
        if cap is not None and int(m.group(1)) != int(cap):
            out.append(
                _violation(
                    "tier-number",
                    f"tier-{unit}-cap",
                    m,
                    text,
                    f"{tier['display_name']} {unit} cap is {cap}, copy says {int(m.group(1))}",
                )
            )
    return out
