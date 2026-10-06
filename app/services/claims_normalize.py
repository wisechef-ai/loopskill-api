"""Text normalisation for the claims contract (claimgate_1006).

Split out of claims_contract.py (600-line module gate). Everything here has a
byte-for-byte twin in deploy/claimgate/install.sql (claimgate.decode_entities
/ claimgate.normalize); tests/test_claims_contract_pg_parity.py proves they
agree, and test_install_sql_patterns_match_python pins the shared literals.
"""

from __future__ import annotations

import re

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
# HTML "legacy" named references that browsers decode WITHOUT a trailing ";"
# ("Pro&nbsp$199"), restricted to names in NAMED_ENTITIES.
LEGACY_NO_SEMICOLON = (
    "nbsp",
    "amp",
    "lt",
    "gt",
    "quot",
    "copy",
    "reg",
    "times",
    "divide",
    "middot",
    "cent",
    "pound",
    "yen",
    "shy",
)
# Numeric tokens are matched WHOLE (any length) and range-checked afterwards,
# so "&#11141110;" stays literal (and is flagged) instead of decoding a prefix.
_ENTITY = re.compile(
    r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[A-Za-z][A-Za-z0-9]{0,31};|(" + "|".join(LEGACY_NO_SEMICOLON) + r"))"
)
# Invisible / space-like characters, mapped identically in install.sql
# (claimgate.normalize generates its translate() lists from these via the
# parity test): ZERO_WIDTH are deleted ("Pro\u200b+" reads "Pro+"), SPACE_LIKE
# become an ASCII space ("Pro\u2003$199" reads "Pro $199").
ZERO_WIDTH = "\u00ad\u200b\u200c\u200d\u2060\ufeff"
SPACE_LIKE = (
    "\u0085\u00a0\u1680" + "".join(chr(c) for c in range(0x2000, 0x200B)) + "\u2028\u2029\u202f\u205f\u3000"
)
_ZW_TABLE = {ord(c): None for c in ZERO_WIDTH} | {ord(c): " " for c in SPACE_LIKE}
# An entity left in the text after the single decoding pass is either outside
# the explicit table (an alias like &NonBreakingSpace; that a browser WOULD
# render) or out of range. Both engines flag it instead of guessing: a
# whitelist cannot be exhaustive, so unknown = violation (fail closed).
UNRECOGNISED_ENTITY = r"&(#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[A-Za-z][A-Za-z0-9]{0,31};)"


def _decode_entity(m: re.Match) -> str:
    body = m.group(1).rstrip(";")
    if body.startswith("#"):
        hexa = body[1:2] in ("x", "X")
        digits = (body[2:] if hexa else body[1:]).lstrip("0") or "0"
        if len(digits) > (6 if hexa else 7):  # > U+10FFFF without a huge int()
            return m.group(0)
        cp = int(digits, 16) if hexa else int(digits)
        if cp == 0 or 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
            return m.group(0)
        return chr(cp)
    return NAMED_ENTITIES.get(body, m.group(0))


def normalize(text: str) -> str:
    """The text BOTH engines check (install.sql: claimgate.normalize).

    1. tags removed WITHOUT inserting a space (``Pro<b>+</b>`` -> ``Pro+``);
    2. entities decoded once, per the explicit spec above;
    3. ZERO_WIDTH deleted, SPACE_LIKE -> space;
    4. ASCII whitespace runs -> one space; trimmed of ASCII spaces only (the
       same thing Postgres btrim() does).
    """
    text = re.sub(r"<[^>]+>", "", text or "")
    text = _ENTITY.sub(_decode_entity, text).translate(_ZW_TABLE)
    return re.sub(r"[ \t\r\n\f\v]+", " ", text).strip(" ")
