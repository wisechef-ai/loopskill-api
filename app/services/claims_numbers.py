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
