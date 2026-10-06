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
  call it over HTTP (``POST /api/marketing/claims/check``); the Postiz
  trigger (deploy/claimgate/install.sql) runs the same rules, tier binding
  included. tests/test_claims_contract_pg_parity.py proves both engines give
  the same EXPECTED verdicts. Threat model: deploy/claimgate/README.md.

Portable regex subset (Python ``re`` and PostgreSQL ARE): ``[0-9]`` never
``\\d``, ``\\b`` (translated to ``\\y``), no lookarounds, backreferences,
named groups, inline flags or ``\\d \\w \\s`` (``assert_portable``).
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

import yaml

# re-exported for tests and install.sql parity
from app.services.claims_normalize import (  # noqa: F401
    _ENTITY,
    ALLOWED_TAG,
    ATTR_VALUE,
    BLOCK_TAG,
    POSTIZ_TAG,
    TAG_READINGS,
    TAGLIKE,
    C1_REMAP,
    LEGACY_NO_SEMICOLON,
    NAMED_ENTITIES,
    SPACE_LIKE,
    UNRECOGNISED_ENTITY,
    ZERO_WIDTH,
    normalize,
    markup_violations,
    strip_tags,
    unsupported_markup,
)

_CONFIG = Path(__file__).resolve().parent.parent.parent / "config"
TIERS_YAML = _CONFIG / "tiers.yaml"
CONTRACT_YAML = _CONFIG / "claims_contract.yaml"

MAX_CHECK_CHARS = 20_000

# A WHOLE numeric token: the trailing \b forbids stopping inside one, so
# "$9,950" can never be read as "$9,95" (it backtracks to "9" and is flagged).
# Unbounded on purpose: float() of a 20k-digit token is inf, never an error.
# A number is only ever read as a WHOLE token:
# * GROUPED "1,199" / "1.050,50" / "1 050" is one token (thousands grouping by
#   comma, period or space, optional 1-2 digit decimals);
# * the trailing \b forbids stopping inside a token ("$9,950" is never "$9,95");
# * _NOT_AFTER_NUM forbids starting inside one ("1,050 private bundles" is
#   never read as "050"): the character before must not be a digit, comma or
#   period. Unbounded on purpose: parse_amount of a 20k-digit token is inf.
# Thousands grouping must use ONE separator throughout, and a decimal part
# must use the OTHER mark (no backreferences in the portable subset, so the
# three shapes are spelled out): 1,199.50 / 1.050,50 / 1 050,50.
_GROUPED = (
    r"([0-9]{1,3}(,[0-9]{3})+([.][0-9]{1,2})?"
    r"|[0-9]{1,3}([.][0-9]{3})+(,[0-9]{1,2})?"
    r"|[0-9]{1,3}( [0-9]{3})+([.,][0-9]{1,2})?)"
)
# The WHOLE run of digits and separators next to a unit/currency is captured
# (never a tail of it); parse_amount then decides whether it is a well-formed
# amount. A malformed run ("1,2,50", "1 50") is a violation, not "no claim"
# (fail closed). Each repetition starts with a separator, so matching stays
# linear.
_RUN = r"[0-9]+([.,][0-9]+| [0-9]+)*"
_NUM = r"(" + _RUN + r")\b"
_COUNT = r"(" + _RUN + r")"
# The character(s) before a number: not a digit, and not a comma / period /
# space that itself follows a digit ("1,050" / "1 050" are one run, never
# "050"). Punctuation after a word is fine ("bundles,20 API keys" reads 20).
# Each run has exactly ONE possible start, which keeps scanning linear.
_NOT_AFTER_NUM = r"(^|[^0-9,. ]|(^|[^0-9])[,. ])"
# capture-group index of the number right after the guard (computed, never
# hard-coded: the guard has its own groups)
NUM_AFTER_GUARD = re.compile(_NOT_AFTER_NUM).groups + 1
THOUSANDS = "^" + _GROUPED + "$"
VALID_AMOUNT = "^(" + _GROUPED + "|[0-9]+([.,][0-9]{1,2})?)$"


def parse_amount(token: str) -> float:
    """Value of a run matched by _NUM / _COUNT (install.sql: claimgate.parse_amount).

    Not a well-formed amount (VALID_AMOUNT) -> NaN, which matches no allowed
    value (SQL: NULL, treated as a violation).

    Grouped: the FIRST separator is the grouping one and is removed
    ("1,199.50" -> 1199.5, "1.050,50" -> 1050.5, "1 050" -> 1050); then a
    remaining comma is a decimal point ("9,95" -> 9.95).
    """
    if not re.match(VALID_AMOUNT, token):
        return float("nan")  # malformed: equals nothing, so always a violation
    if re.match(THOUSANDS, token):
        token = token.replace(re.search(r"[., ]", token).group(0), "")
    try:
        return float(token.replace(",", "."))
    except ValueError:  # defensive: never raise from the check
        return float("nan")


_RECURRING = (
    r"(/ ?mo|/ ?m|/ ?month|per month|a month|monthly|/ ?yr|/ ?year|per year|a year|annually|yearly)\b"
)
_ONE_TIME = r"(one-time|one time|once|lifetime)\b"
# An API-key count: "1 API key", "10 API keys", "20 keys", "20 scoped keys".
# Plural "keys" after a number is always a count (fail closed); singular "key"
# only with "API" or a qualifier, so "3 key lessons" is not a key count.
_KEY_UNIT = r"((active |scoped |separate |client )?(API )?keys|(active |scoped |separate |client )?API key|(active|scoped|separate|client) key)"
# Only explicit connectors bind a price to a tier ("Pro is $X", "Pro at $X",
# "Pro: $X", "Pro plan for $X"), so "Pro saved $20 in API spend" is not a price.
_TIER_LINK = (
    r"( plan| tier)?,?( is| costs| cost| at| for| from| only| just| now| starts| starting| priced| still| runs| goes){0,3}"
    r":? ?[-–—]? ?"
)

_PORTABLE_FORBIDDEN = re.compile(r"\(\?|\\[1-9]|\\[AZzGkpPNdDwWsS]")


def to_pg(pattern: str) -> str:
    """Translate a portable pattern to PostgreSQL ARE (word boundary only)."""
    return pattern.replace(r"\b", r"\y")


def assert_portable(pattern: str) -> None:
    """Raise ValueError if ``pattern`` uses syntax the Postgres trigger can't run identically."""
    if _PORTABLE_FORBIDDEN.search(pattern):
        raise ValueError(f"non-portable regex construct in {pattern!r}")
    re.compile(pattern, re.IGNORECASE)


def _lit(name: str) -> str:
    """A literal name as a portable pattern (spaces and hyphens unescaped)."""
    return re.escape(name).replace(r"\ ", " ").replace(r"\-", "-")


_CADENCE = {
    "monthly": r"(/ ?mo|/ ?m|/ ?month|per month|a month|monthly)\b",
    "one-time": r"(one-time|one time|once|lifetime)\b",
}


def _exempt_rule(i: int, op: dict, veto: str) -> dict:
    """Exemption for ANOTHER product's price, generated (never hand-written).

    Matches "<product_name> ... <complete amount> <cadence>" inside one
    sentence, so "$199.95/month" or "$199 one-time" never qualify for a
    monthly $199. The window between the name and the price holds no digits,
    so "1,199" / "1 199" / "1199" can never lend their tail "199". The veto (LoopSkill or any tier name inside the span) keeps
    "WiseChef integrates with LoopSkill which costs $199/month" a LoopSkill
    price claim.
    """
    cadence = op.get("cadence", "monthly")
    if not op.get("product_name") or cadence not in _CADENCE:
        raise ValueError(f"other_prices entry needs product_name and cadence in {sorted(_CADENCE)}: {op!r}")
    amt = re.escape(_num(op["amount_usd"]))
    cad = _CADENCE[cadence]
    return {
        "id": f"other-price-{i:02d}",
        "pattern": (
            r"\b" + _lit(op["product_name"]) + r"\b[^0-9.!?$€]{0,120}("
            r"[$€] ?"
            + amt
            + r" ?(USD|EUR)? ?"
            + cad
            + r"|\b"
            + amt
            + r" ?(USD|EUR|dollars|euros) ?"
            + cad
            + ")"
        ),
        "veto": veto,
        "reason": f"{op.get('product')} ({op.get('evidence')})",
    }


def _apply_exemptions(text: str, c: dict) -> str:
    """Blank out other products' own price claims.

    The veto is checked against the WHOLE SENTENCE around the match (not just
    the matched span), so "LoopSkill costs, unlike WiseChef, $199/month." and
    "WiseChef costs $199/month, and so does LoopSkill." are never exempted.
    Step-by-step scan on the progressively blanked text, identical to
    claimgate.violations in install.sql.
    """
    for ex in c.get("exempt_rules") or []:
        pat = re.compile(ex["pattern"], re.IGNORECASE)
        veto = re.compile(ex["veto"], re.IGNORECASE)
        pos = 0
        while True:
            m = pat.search(text, pos)
            if not m:
                break
            ends = [e.end() for e in _SENTENCE_END.finditer(text, 0, m.start())]
            sstart = ends[-1] if ends else 0
            nxt = _SENTENCE_END.search(text, m.end())
            send = nxt.start() if nxt else len(text)
            if veto.search(text[sstart:send]):
                pos = m.end()
            else:
                text = text[: m.start()] + " " + text[m.end() :]
                pos = m.start() + 1
    return text


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
    # Other products' prices are NOT added here: they are allowed only inside
    # their own product context (exempt_rules), never as a free-floating amount.
    one_time = {0.0}
    if founding and founding.get("price_usd") is not None:
        one_time.add(float(founding["price_usd"]))
    rules = [
        {
            "id": "price-recurring",
            "exemptable": True,
            "pattern": r"[$€] ?" + _NUM + " ?(USD|EUR)? ?" + _RECURRING,
            "amount_group": 1,
            "allowed": sorted(recurring),
            "reason": "recurring price not on the public ladder",
        },
        {
            "id": "price-recurring-suffix",
            "exemptable": True,
            "pattern": _NOT_AFTER_NUM + _NUM + r" ?(USD|EUR|dollars|euros|bucks) ?" + _RECURRING,
            "amount_group": NUM_AFTER_GUARD,
            "allowed": sorted(recurring),
            "reason": "recurring price not on the public ladder",
        },
        {
            "id": "price-one-time",
            "exemptable": True,
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
            "pattern": _NOT_AFTER_NUM + _COUNT + r" private bundles?\b",
            "amount_group": NUM_AFTER_GUARD,
            "allowed": sorted(bundle_caps),
            "reason": "private-bundle count is no tier's cap",
        },
        {
            "id": "count-api-keys",
            "pattern": _NOT_AFTER_NUM + _COUNT + " " + _KEY_UNIT + r"\b",
            "amount_group": NUM_AFTER_GUARD,
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

    veto_names = ["LoopSkill", "Founding", "On-demand", "Enterprise"] + [
        str(cfg.get("display_name", slug)) for slug, cfg in tiers.items()
    ]
    veto = r"\b(" + "|".join(sorted({_lit(n) for n in veto_names}, key=len, reverse=True)) + r")"
    exempt = [_exempt_rule(i, op, veto) for i, op in enumerate(other_prices)]
    retired = (
        _derived_retired(tiers)
        + [
            {
                "id": "unrecognised-html-entity",
                "pattern": UNRECOGNISED_ENTITY,
                "reason": "HTML entity outside the decoding table (or out of range): its rendered text "
                "cannot be checked, so it is rejected. Write the character itself.",
                "replacement": "the plain character",
                "source": "app/services/claims_contract.py",
            }
        ]
        + [
            {k: r.get(k) for k in ("id", "pattern", "reason", "replacement", "since")}
            | {"source": "config/claims_contract.yaml"}
            for r in contract_doc.get("rules") or []
        ]
    )
    amounts = _amount_rules(public, founding, other_prices)
    for r in retired + amounts + exempt:
        assert_portable(r["pattern"])
        r["pg_pattern"] = to_pg(r["pattern"])
        if "veto" in r:
            assert_portable(r["veto"])
            r["pg_veto"] = to_pg(r["veto"])
        r["reason"] = " ".join(str(r.get("reason") or "").split())

    body = {
        "contract_version": 4,
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
        "exempt_rules": exempt,
        "check_endpoint": "/api/marketing/claims/check",
    }
    body["contract_hash"] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return json.dumps(body)


def build_contract() -> dict:
    """Return the claims contract. Cached until either yaml file changes."""
    return json.loads(_cached_contract(TIERS_YAML.stat().st_mtime, CONTRACT_YAML.stat().st_mtime))


def _scan(rx: re.Pattern, text: str):
    """All matches of ``rx``, left to right, restarting ONE character before
    the end of each match: the last character of a match ("...bundles") may be
    the guard of the next one (",20 API keys"). install.sql loops identically
    (pos := p + greatest(length(tok) - 1, 1)). Never yields the same match
    twice: every pattern scanned here ends in a letter or digit that cannot
    start a new match."""
    pos = 0
    while True:
        m = rx.search(text, pos)
        if not m:
            return
        yield m
        pos = max(m.end() - 1, m.start() + 1)


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
    3. tier binding (also in install.sql claimgate.tier_binding): a bundle/key count is bound to the NEAREST
       preceding tier name in the same sentence and must equal that tier's
       cap. "Free users can upgrade to Pro for 50 private bundles" binds 50 to
       Pro; "Pro includes 50 private bundles and 20 API keys" flags the 20.
    """
    c = contract or build_contract()
    out: list[dict] = []
    seen: set[tuple] = set()
    for v in markup_violations(text):  # fail closed on markup outside the allowlist
        seen.add((v["rule_id"], v["match"]))
        out.append(v)
    for reading in TAG_READINGS:  # every consumer's reading of the tags (claims_normalize)
        for v in _check_normalized(normalize(text, reading), c):
            key = (v["rule_id"], v["match"])
            if key not in seen:
                seen.add(key)
                out.append(v)
    return out


def _check_normalized(text: str, c: dict) -> list[dict]:
    out: list[dict] = []
    for rule in c["retired_rules"]:
        for m in re.finditer(rule["pattern"], text, re.IGNORECASE):
            out.append(_violation("retired", rule["id"], m, text, rule["reason"], rule.get("replacement")))
    # Generic price rules skip spans that state ANOTHER product's price in that
    # product's own context ("WiseChef ... from $199/month"). Tier-bound rules
    # still see the full text, so "WiseChef: Pro costs $199/month" is flagged.
    exempted = _apply_exemptions(text, c)
    for rule in c["amount_rules"]:
        allowed = {round(float(a), 2) for a in rule["allowed"]}
        src = exempted if rule.get("exemptable") else text
        for m in _scan(re.compile(rule["pattern"], re.IGNORECASE), src):
            amount = round(parse_amount(m.group(rule["amount_group"])), 2)
            if amount not in allowed:
                shown = ", ".join(_num(a) for a in sorted(allowed)) or "none (contact only)"
                out.append(
                    _violation(
                        "amount", rule["id"], m, src, f"{rule['reason']}: {_num(amount)} (allowed: {shown})"
                    )
                )
    out += _tier_binding(text, c)
    return out


_SENTENCE_END = re.compile(r"[.!?] ")
_UNIT = re.compile(_NOT_AFTER_NUM + _COUNT + r" (private bundles?|" + _KEY_UNIT + r")\b", re.IGNORECASE)
# capture groups: 1 = guard, NUM_AFTER_GUARD = count, UNIT_GROUP = unit word.
# Computed from the regex; install.sql tier_binding reads the same indices
# (pinned by test_install_sql_patterns_match_python).
UNIT_GROUP = re.compile(_NOT_AFTER_NUM + _COUNT).groups + 1


def _tier_binding(text: str, c: dict) -> list[dict]:
    tiers = {t["display_name"].lower(): t for t in c["public_tiers"]}
    if not tiers:
        return []
    name_re = re.compile(r"\b(" + "|".join(re.escape(n) for n in tiers) + r")\b", re.IGNORECASE)
    out = []
    for m in _scan(_UNIT, text):
        # the count starts AFTER the guard char; sentence start = after the
        # last ".", "!" or "?" FOLLOWED BY a space ("$9.95" is no sentence end)
        cstart = m.start(NUM_AFTER_GUARD)
        ends = [e.end() for e in _SENTENCE_END.finditer(text, 0, cstart)]
        start = ends[-1] if ends else 0
        names = list(name_re.finditer(text, start, cstart))
        if not names:
            continue
        tier = tiers[names[-1].group(1).lower()]
        unit = "key" if "key" in m.group(UNIT_GROUP).lower() else "bundle"
        cap = tier.get("api_key_cap") if unit == "key" else tier.get("bundle_limit")
        count = parse_amount(m.group(NUM_AFTER_GUARD))
        if cap is not None and count != float(cap):
            shown = _num(count) if count == count and count != float("inf") else m.group(NUM_AFTER_GUARD)
            out.append(
                _violation(
                    "tier-number",
                    f"tier-{unit}-cap",
                    m,
                    text,
                    f"{tier['display_name']} {unit} cap is {cap}, copy says {shown}",
                )
            )
    return out
