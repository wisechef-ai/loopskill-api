"""Number grammar shared by the claims gate (split from claims_contract for
the 600-line module cap). install.sql mirrors every pattern here; the
literals are pinned by tests/test_claims_contract.py."""

import re

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


# An API-key count: "1 API key", "10 API keys", "20 keys", "20 scoped keys".
# Plural "keys" after a number is always a count (fail closed); singular "key"
# only with "API" or a qualifier, so "3 key lessons" is not a key count.
_KEY_UNIT = r"((active |scoped |separate |client )?(API )?keys|(active |scoped |separate |client )?API key|(active|scoped|separate|client) key)"


# Public tier prices are MONTHLY; an annual price may only use an amount
# tiers.yaml defines as annual_price_usd (round 21: "Pro is $9.95/year").
# A billing period is <lead><unit>, so EVERY lead combines with every unit
# ("each month", "every year", "for each week"; round 24).
# An optional qualifier between lead and unit: "per calendar year", "every
# billing month", "a full year" (round 26).
_LEAD = (
    r"(for |billed |paid |charged )?(/ ?|per |a |an |each |every )"
    r"((calendar|fiscal|financial|billing|full|whole|single|entire|subscription) )?"
)
# An optional per-seat unit before the period: "$19 per user per month",
# "$19/seat/mo" (round 25). It sits AFTER the amount, so no amount group
# index moves.
_PER_UNIT = (
    r"((/ ?|per |a |an |each |every )"
    r"(users?|seats?|members?|agents?|persons?|people|heads?|licen[cs]es?|accounts?|workspaces?) ?)?"
)
_MONTHLY = _PER_UNIT + r"(" + _LEAD + r"(months?|mos?|mths?)|/ ?m|monthly|month-to-month)\b"
_ANNUAL = _PER_UNIT + r"(" + _LEAD + r"(years?|yrs?|annum)|annually|yearly)\b"
_FILLER = (
    r"( (of|around|about|over|up|to|nearly|almost|roughly|approximately|more|than|least|an|a|the|average"
    r"|exceeding|topping|upwards|north|totaling|totalling|reaching|well"
    r"|estimated|them|you|your|team|teams|businesses|companies|clients|customers)){0,5}"
)
# Any OTHER billing period is unsupported (round 23: "$9.95/week").
_OTHER_PERIOD = _PER_UNIT + (
    r"("
    + _LEAD
    + r"(wks?|weeks?|days?|hrs?|hours?|minutes?|mins?|quarters?|qtrs?|fortnights?|semesters?|decades?)"
    r"|weekly|daily|hourly|quarterly|biweekly|bi-weekly|fortnightly|semiannually|semi-annually)\b"
)


# Every currency marker a price can carry (round 24: "£999/month"). The gate
# checks the AMOUNT against tiers.yaml whatever the marker, so a foreign
# currency never hides a price. Prefix ends in an optional space.
_CUR_PRE = r"([$€£¥₹]|(US|A|C|NZ|HK|S|R)\$|(USD|EUR|GBP|PLN|CHF|CAD|AUD|JPY|INR) ?) ?"
# Words end at a word boundary ("USDC" is no currency); symbols may sit
# right after the amount, the European way ("19 €/month", "19€"; round 25).
_CUR_SUF = (
    r"((USD|EUR|GBP|PLN|CHF|CAD|AUD|JPY|INR|zł|zloty|złoty|dollars|euros|pounds|quid|bucks|francs)\b|[€£¥₹$])"
)


def num_group(prefix: str) -> int:
    """Capture-group index of the number that follows ``prefix`` (computed from
    the regex itself, never hard-coded: currency and guards carry groups)."""
    return re.compile(prefix, re.IGNORECASE).groups + 1


_ONE_TIME = r"(one-time|one time|once|lifetime)\b"
# Only explicit connectors bind a price to a tier ("Pro is $X", "Pro at $X",
# "Pro: $X", "Pro plan for $X"), so "Pro saved $20 in API spend" is not a price.
_TIER_LINK = (
    r"( plan| tier)?,?( is| costs| cost| at| for| from| only| just| now| starts| starting| priced| still| runs| goes){0,3}"
    r":? ?[-–—]? ?"
)
ATTACH = r"^,? (on|with|in|for|under) (the |a |an |your |our )?"
