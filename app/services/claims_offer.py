"""Offer-shape claim rules derived from config/tiers.yaml (eval-corpus round 2).

The measured corpus (tests/test_claims_eval_corpus.py) showed whole categories
the amount rules could not see, because the false claim carries no price:
- a billing PERIOD that does not exist ("an annual Pro plan", "renews every
  six months", "billed weekly"): LoopSkill bills monthly only;
- Founding Member TERMS: the seat cap ("500 Founding seats") and the term
  ("Pro for a year" when the SKU is Pro for life);
- a non-USD price ("Pro is €9.95 a month"): every public price is USD;
- retired VOCABULARY on its own ("Publish your cookbook", "Discover
  Recipes"), outside a real kitchen context (WiseChef's restaurant copy).
Every rule is generated, portable to the Postgres trigger, and pinned by the
corpus ratchet plus attack fixtures in tests/test_claims_contract.py.
"""

from __future__ import annotations

from app.services.claims_numbers import (
    _ANNUAL,
    _COUNT,
    _MONTHLY,
    _NOT_AFTER_NUM,
    _NUM,
    _ONE_TIME,
    _OTHER_PERIOD,
    num_group,
)

# Every billing period other than monthly, as an adjective / adverb.
PERIOD_ADJ = (
    r"(annual|yearly|weekly|daily|quarterly|biweekly|bi-weekly|fortnightly|semi-annual|semiannual"
    r"|six-month|6-month|twelve-month|12-month|two-year|2-year|multi-year|three-month|3-month)"
)
PERIOD_ADV = (
    r"(annually|yearly|weekly|daily|quarterly|biweekly|bi-weekly|fortnightly|semiannually|semi-annually)"
)
# What a billing period can qualify. "plan" is a billing noun only for the
# long periods: "a weekly plan" is just as often a content plan.
BILLING_NOUN = (
    r"(billing( cycles?| options?)?|subscriptions?|pricing|commitment|contracts?|payments?|fees?|pass"
    r"|memberships?|rates?|deals?|discounts?|access|licen[cs]es?|options?|cycles?)"
)
_LONG_ADJ = r"(annual|yearly|quarterly|semi-annual|semiannual|six-month|6-month|twelve-month|12-month|two-year|2-year|multi-year)"
_EVERY = (
    r"(every|each|per|once a|once per|once every) "
    r"((year|week|day|quarter|fortnight)|(two|three|four|six|twelve|[2-9]|1[0-9]|2[0-4]) (weeks|months|years))"
)
_BILL_VERB = r"(renews?|renewal|bills?|billed|billing|charges?|charged|invoiced|pays?|paid|payable)"


def billing_period_pattern() -> str:
    """Any claim of a billing period other than monthly (exemptable: a NEGATED
    mention is stripped by negation_exemptions)."""
    return (
        r"\b("
        + PERIOD_ADJ
        + "( (pro|free|loopskill|founding))? "
        + BILLING_NOUN
        + "|"
        + _LONG_ADJ
        + "( (pro|loopskill))? plans?"
        + r"|(offers?|offering|introducing|new|our|buy|choose|pick|get)( an?)? "
        + r"(weekly|daily|biweekly|fortnightly) (plan|tier)"
        + "|"
        + _BILL_VERB
        + "( (pro|you|users|teams|customers|members))? "
        + _EVERY
        + "|"
        + _BILL_VERB
        + " "
        + PERIOD_ADV
        + r"|per annum"
        + r"|(monthly|month) (or|and|/) (annual|annually|yearly)|(annual|annually|yearly) (or|and|/) monthly"
        + r"|billing:? (annual|annually|yearly)"
        + r")\b"
    )


def negated_period_pattern() -> str:
    """'There is no annual plan', 'we don't offer yearly billing', 'never
    billed weekly': negator + auxiliary words + a period claim."""
    neg = r"(\bno|\bnot|\bnever|\bwithout|n't)"
    aux = r"( (offer|offering|have|has|sell|provide|do|does|currently|yet|any|an|a|the|separate|need|require|be))*"
    what = (
        " ("
        + PERIOD_ADJ
        + "( (pro|free|loopskill))?( "
        + BILLING_NOUN
        + "| plans?)?|"
        + _BILL_VERB
        + " ("
        + PERIOD_ADV
        + "|"
        + _EVERY
        + r"))\b"
    )
    return neg + aux + what


# ---------------------------------------------------------------- currency
# Every public LoopSkill price is USD (app.loopskill.io/pricing declares
# priceCurrency USD), so a non-USD marker on a non-zero price is false.
# "US$" must never read as a foreign "S$": only unambiguous markers here.
NON_USD_PRE = r"([€£¥₹]|(A|C|NZ|HK)\$|(EUR|GBP|PLN|CHF|CAD|AUD|JPY|INR) ?) ?"
NON_USD_SUF = r"((EUR|GBP|PLN|CHF|CAD|AUD|JPY|INR|zł|zloty|złoty|euros|pounds|quid|francs)\b|[€£¥₹])"
# Speed only (Python): an amount rule whose every match contains one of these
# is skipped when the text has none (claims_contract._check_normalized).
_NON_USD_MARK = (
    r"[€£¥₹]|(A|C|NZ|HK)\$|EUR|GBP|PLN|CHF|CAD|AUD|JPY|INR|zł|zloty|złoty|euros|pounds|quid|francs"
)
_AS = r"(as an? |for an? |in an? |with an? )?"


def non_usd_rules() -> list[dict]:
    cadence = "(" + "|".join((_MONTHLY, _ANNUAL, _OTHER_PERIOD, _ONE_TIME)) + ")"
    reason = "LoopSkill prices are USD only (app.loopskill.io/pricing): a non-USD price is not a real price"
    return [
        {
            "id": "price-non-usd",
            "requires": _NON_USD_MARK,
            "pattern": NON_USD_PRE + _NUM + " ?" + NON_USD_SUF + "? ?" + _AS + cadence,
            "amount_group": num_group(NON_USD_PRE),
            "allowed": [0.0],
            "reason": reason,
            "exemptable": True,
        },
        {
            "id": "price-non-usd-suffix",
            "requires": NON_USD_SUF,
            "pattern": _NOT_AFTER_NUM + _NUM + " ?" + NON_USD_SUF + " ?" + _AS + cadence,
            "amount_group": num_group(_NOT_AFTER_NUM),
            "allowed": [0.0],
            "reason": reason,
            "exemptable": True,
        },
    ]


# ---------------------------------------------------------------- Founding
_SEAT = r"(seats?|spots?|places?|slots?|people|members|memberships?|passes)"


def founding_rules(founding: dict | None) -> tuple[list[dict], list[dict]]:
    """(amount rules, retired rules) for the Founding Member SKU."""
    if not founding:
        return [], []
    amounts: list[dict] = []
    cap = founding.get("slot_cap")
    if cap is not None:
        reason = f"Founding Member is capped at {int(cap)} seats (config/tiers.yaml slot_cap)"
        pre_f2 = r"\bfounding\b[^.!?]{0,80}" + _NOT_AFTER_NUM
        pre_f3 = r"\bfounding\b[^.!?]{0,60}\b(capped|limited|limit|cap)( is| of| at| to){0,2} "
        for rid, pattern, group in (
            # "500 Founding Member seats", "one of 75 Founding Members"
            (
                "founding-seats",
                _NOT_AFTER_NUM + _COUNT + r" founding( member)? " + _SEAT + r"\b",
                num_group(_NOT_AFTER_NUM),
            ),
            # "Founding Member: $49 one-time, limited to 200 seats"
            (
                "founding-seats-after",
                pre_f2 + _COUNT + " (total |founding )?" + _SEAT + r"\b",
                num_group(pre_f2),
            ),
            # "Founding Member seats are capped at 150"
            ("founding-seats-cap", pre_f3 + _COUNT + r"\b", num_group(pre_f3)),
            # "Only 25 seats are open in the Founding offer"
            (
                "founding-seats-before",
                _NOT_AFTER_NUM + _COUNT + r" (seats?|spots?|places?|slots?)\b[^.!?]{0,60}\bfounding\b",
                num_group(_NOT_AFTER_NUM),
            ),
        ):
            amounts.append(
                {
                    "id": rid,
                    "pattern": pattern,
                    "requires": "founding",
                    "amount_group": group,
                    "allowed": [float(cap)],
                    "reason": reason,
                }
            )
    n = r"(a|one|two|three|four|five|six|ten|twelve|[0-9]+)"
    term = r"(pro for " + n + r" (years?|months?)|" + n + r" (years?|months?) of pro)\b"
    retired = [
        {
            "id": "founding-not-lifetime",
            "pattern": r"\bfounding\b[^.!?]{0,80}\b" + term + r"|\b" + term + r"[^.!?]{0,80}\bfounding\b",
            "reason": "Founding Member is Pro for life (one-time payment), not a fixed term",
            "replacement": "Pro for life, one-time payment",
            "source": "config/tiers.yaml",
        }
    ]
    return amounts, retired


# ---------------------------------------------------------------- vocabulary
_KITCHEN = (
    r"(kitchens?|restaurants?|menus?|chefs?|food|dish|dishes|cuisine|culinary|cooking|baking|bake|meals?"
    r"|ingredients?|diners?|catering|hospitality|dinner|lunch|breakfast)"
)
_WORD = r"(cookbooks?|recipes)"


def vocab_rules() -> tuple[list[dict], list[dict]]:
    """(retired rules, exempt rules): "cookbook(s)" and "recipes" are retired
    LoopSkill vocabulary (renamed to bundles / LoopSkill) except in a real
    kitchen sentence. The kitchen window holds no digits or currency, so the
    exemption can never hide a price."""
    retired = [
        {
            "id": "retired-vocab-cookbook-recipes",
            "pattern": r"\b" + _WORD + r"\b",
            "reason": '"cookbooks" were renamed to bundles and "Recipes" to LoopSkill',
            "replacement": '"bundles" / "LoopSkill" (kitchen copy is exempt)',
            "source": "app/services/claims_offer.py",
            "exemptable": True,
        }
    ]
    gap = r"\b[^.!?0-9$€£¥₹]{0,100}\b"
    exempt = [
        {
            "id": "kitchen-vocab",
            "pattern": r"\b" + _KITCHEN + gap + _WORD + r"\b|\b" + _WORD + gap + _KITCHEN + r"\b",
            "veto": r"\b(LoopSkill|bundles?|SKILL\.md|loopskill\.io|marketplace|catalog|install|installs|publish)\b",
            "reason": "kitchen copy: a cookbook / recipes in the culinary sense",
        }
    ]
    return retired, exempt


def tier_adjectival_rule(
    rule_id: str, name_pattern: str, tail: str, allowed: list[float], reason: str
) -> dict:
    """'$99 Founding pass', '$20 Pro plan': a price used as an adjective."""
    from app.services.claims_numbers import _CUR_PRE, _CUR_SUF

    period = "(" + "|".join((_MONTHLY, _ANNUAL, _ONE_TIME)) + ")"
    return {
        "id": rule_id + "-adjectival",
        "pattern": _CUR_PRE + _NUM + " ?" + _CUR_SUF + "? ?(" + period + " )?" + r"\b" + name_pattern + tail,
        "amount_group": num_group(_CUR_PRE),
        "allowed": sorted(allowed),
        "reason": reason,
    }
