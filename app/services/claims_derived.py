"""Retired-claim rules DERIVED from config/tiers.yaml (split from
claims_contract for the 600-line module cap). Each rule exists only while
tiers.yaml makes the claim false, so editing tiers.yaml retires or revives it.
Rules are data: install.sql runs them unchanged via the contract sync."""

import re

from app.services.claims_numbers import (
    _ANNUAL,
    _CUR_PRE,
    _CUR_SUF,
    _FILLER,
    _KEY_UNIT,
    _MONTHLY,
    _NOT_AFTER_NUM,
    _NUM,
    _ONE_TIME,
    _OTHER_PERIOD,
    _TIER_LINK,
    ATTACH,
    NUM_AFTER_GUARD,
    num_group,
)


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


def derived_fact_rules(public: list[dict]) -> list[dict]:
    """Claims no public tier supports: annual billing, unlimited private
    bundles, unlimited API keys (rounds 21-22)."""
    rules = []
    if all(t.get("annual_price_usd") is None for t in public):
        rules.append(
            {
                "id": "annual-billing-not-offered",
                "pattern": r"\b((billed|paid|charged|invoiced|payable) (annually|yearly|per year|once a year|per annum)"
                r"|pay (annually|yearly)|per annum"
                r"|(annual|yearly) (billing|plans?|subscriptions?|pricing|commitment|contracts?|payments?))\b",
                # a NEGATED mention ("there is no annual plan") is exempt
                # (negation_exemptions); the sentence veto keeps savings copy
                "exemptable": True,
                "reason": "no tier in config/tiers.yaml has an annual_price_usd: LoopSkill bills monthly",
                "replacement": "per-month pricing from app.loopskill.io/pricing",
                "source": "config/tiers.yaml",
            }
        )
    many = r"(unlimited|infinite|limitless|uncapped|endless) "
    as_many = r"as many "
    want = r" as you (want|like|need)\b"
    for key, unit, rid in (
        # "private bundles", "private and public bundles", "public and private
        # bundles", "private/public bundles" (round 23)
        (
            "bundle_limit",
            r"([a-z]+ ?(and|or|&|/) ?)?private( ?(and|or|&|/) ?[a-z]+)? bundles?",
            "unlimited-private-bundles",
        ),
        ("api_key_cap", _KEY_UNIT, "unlimited-api-keys"),
    ):
        if all(t.get(key) is not None for t in public):
            rules.append(
                {
                    "id": rid,
                    "pattern": r"\b("
                    + many
                    + unit
                    + r"|no (limit|cap) on "
                    + unit
                    + "|"
                    + as_many
                    + unit
                    + want
                    + ")",
                    "reason": f"every public tier in config/tiers.yaml has a finite {key}",
                    "replacement": "the tier's cap from app.loopskill.io/pricing",
                    "source": "config/tiers.yaml",
                }
            )
    return rules


def loss_exemptions(veto: str) -> list[dict]:
    """Loss / savings / value figures ("losses of around $8,000 a day", "saving
    them an average of $150,000 per year", "a $50,000 weekly loss") are not
    prices. Tight on purpose: only _FILLER words may sit between the trigger
    word and the amount ("just", "only", "for", "with" never do), and the
    sentence veto (LoopSkill / any tier / any commerce word) still applies
    (rounds 23, 26)."""
    period = "(" + "|".join((_MONTHLY, _ANNUAL, _OTHER_PERIOD)) + ")"
    amount = _CUR_PRE + _NUM + r" ?" + _CUR_SUF + "? ?"
    trigger = r"\b(lose|loses|losing|lost|losses|loss|bleeding|burning|wasting|save|saves|saved|saving|worth)"
    # A sentence about what anyone CHARGES is a price sentence, never a loss
    # figure: "Our subscription is worth $199/month, and that's what we
    # charge" (round 26). Commerce words join the LoopSkill / tier veto.
    # "plan" counts only as a noun ("our plan", "plans start"), never the verb
    # ("plan for resilience").
    commerce = (
        r"|\b(subscriptions?|subscribe|pricing|priced|prices?|charges?|charged|charging|billed|billing"
        r"|tiers?|memberships?|upgrade|checkout|sign up|signup|bundles?|api keys?|seats?)\b"
        r"|\b(our|the|a|your|this|that|paid|monthly|annual|yearly|pro|free) plans?\b"
        r"|\bplans? (start|starts|from|costs?|is|are|at|begin|begins)\b"
    )
    # Cost / spend figures anchored to an AUDIENCE subject ("Downtime costs
    # teams $8,000 a day", "Teams spend $300 a month on ..."). Never "costs
    # you": "It costs you $9.95/week" is a price (round 23 fixture).
    aud = (
        r"(teams?|companies|company|businesses|business|engineers?|developers?|agencies|agency"
        r"|organi[sz]ations?|startups?|founders?|enterprises?|brands?|stores?|restaurants?|owners?|employees|staff)"
    )
    spend = (
        r"\b((cost|costs|costing) "
        + aud
        + "|"
        + aud
        + r" (spend|spends|spent|spending|waste|wastes|wasted|burn|burns))"
    )
    return [
        {
            "id": rid,
            "pattern": pat,
            "veto": veto + commerce,
            "reason": "a loss / savings / value figure, not a price",
        }
        for rid, pat in (
            ("loss-figure-before", trigger + _FILLER + " " + amount + period),
            ("loss-figure-spend", spend + _FILLER + " " + amount + period),
            (
                "loss-figure-after",
                amount + period + r" ?(in |of )?(missed |lost |wasted |forgone |extra |new )?"
                r"(lost|loss|losses|downtime|revenue|sales|income|profits?|savings)\b",
            ),
        )
    ]


def negation_exemptions(retired: list[dict]) -> list[dict]:
    """A NEGATED retired claim is true copy: "There is no annual plan", "We
    don't offer yearly billing", "never billed annually" (eval corpus false
    positives). Only exemptable retired rules see the stripped text. Tight on
    purpose: the negator may be followed only by a few auxiliary words, and a
    sentence that sells savings / discounts / a launch is vetoed, so
    "No catch: annual plans save 20%" stays flagged."""
    if not any(r["id"] == "annual-billing-not-offered" for r in retired):
        return []
    neg = r"(\bno|\bnot|\bnever|\bwithout|n't)"
    aux = r"( (offer|offering|have|has|sell|provide|do|does|currently|yet|any|an|a|the|separate|need|require|be))*"
    what = (
        r" ((annual|yearly)( (billing|plans?|subscriptions?|pricing|commitment|contracts?|payments?|option))?"
        r"|(billed|paid|charged|invoiced) (annually|yearly|per year|per annum))\b"
    )
    return [
        {
            "id": "negated-annual",
            "pattern": neg + aux + what,
            "veto": r"%|\b(save|saves|saving|savings|discount|discounts|discounted|cheaper|introducing|launch|launches"
            r"|launching|coming soon|now offer|now offers)\b",
            "reason": "a negated mention of annual billing is true copy",
        }
    ]


def tier_price_rules(rule_id: str, name_pattern: str, allowed: list[float], label: str) -> list[dict]:
    """Two rules per tier: "Pro is $X" (prefix currency) and "Pro costs X USD" (suffix)."""
    # A trailing \b after a name ending in punctuation ("Pro\+") could never
    # match before a space; only names ending in a word character get one.
    tail = r"\b" if re.search(r"[A-Za-z0-9_)?]$", name_pattern) else ""
    head = r"\b" + name_pattern + tail + _TIER_LINK
    # amount group = every capture group before the number, counted by the
    # regex engine itself (escaped literal parentheses do not count)
    before = re.compile(head).groups
    reason = f"price stated for {label} does not match its tier"
    period = "|".join((_MONTHLY, _ANNUAL, _ONE_TIME))
    return [
        {
            "id": rule_id,
            "pattern": head + _CUR_PRE + _NUM,
            "amount_group": num_group(head + _CUR_PRE),
            "allowed": sorted(allowed),
            "reason": reason,
        },
        {
            "id": rule_id + "-suffix",
            "pattern": head + _NUM + r" ?" + _CUR_SUF,
            "amount_group": before + 1,
            "allowed": sorted(allowed),
            "reason": reason,
        },
        {  # "$9.95/month on the Free tier" (round 22): a currency PREFIX
            "id": rule_id + "-trailing",
            "pattern": _NOT_AFTER_NUM
            + _CUR_PRE
            + _NUM
            + " ?"
            + _CUR_SUF
            + "? ?("
            + period
            + r")?"
            + ATTACH[1:]
            + r"\b"
            + name_pattern
            + tail,
            "amount_group": num_group(_NOT_AFTER_NUM + _CUR_PRE),
            "allowed": sorted(allowed),
            "reason": reason,
        },
        {  # "9.95 USD on Pro", "9.95/month on Pro": a currency SUFFIX or a
            # billing period. A bare count ("2 on Free, 50 on Pro") is never a
            # price (eval corpus false positive).
            "id": rule_id + "-trailing-suffix",
            "pattern": _NOT_AFTER_NUM
            + _NUM
            + " ?("
            + _CUR_SUF
            + " ?("
            + period
            + ")?|("
            + period
            + "))"
            + ATTACH[1:]
            + r"\b"
            + name_pattern
            + tail,
            "amount_group": NUM_AFTER_GUARD,
            "allowed": sorted(allowed),
            "reason": reason,
        },
    ]
