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


# HTML spec "numeric character reference end state": references 128-159
# render as their Windows-1252 characters (#128 shows the euro sign). Codes
# without a mapping stay as-is. Mirrored as claimgate.decode_entities' c1 array.
C1_REMAP: dict[int, str] = {
    0x80: "€", 0x82: "‚", 0x83: "ƒ", 0x84: "„", 0x85: "…", 0x86: "†", 0x87: "‡",
    0x88: "ˆ", 0x89: "‰", 0x8A: "Š", 0x8B: "‹", 0x8C: "Œ", 0x8E: "Ž", 0x91: "‘",
    0x92: "’", 0x93: "“", 0x94: "”", 0x95: "•", 0x96: "–", 0x97: "—", 0x98: "˜",
    0x99: "™", 0x9A: "š", 0x9B: "›", 0x9C: "œ", 0x9E: "ž", 0x9F: "Ÿ",
}  # fmt: skip


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
        return C1_REMAP.get(cp, chr(cp))
    return NAMED_ENTITIES.get(body, m.group(0))


# A tag, with quoted attribute values allowed to contain ">" ('<b title=">">').
# Each alternative starts with a distinct character, so matching is linear.
TAG = r"<([^>\"']|\"[^\"]*\"|'[^']*')*>"
# Elements a browser displays as blocks / line breaks by default (the HTML
# rendering spec's default stylesheet). Quoted attributes allowed, as in TAG.
BLOCK_TAG = (
    r"<[ \t\r\n]*/?[ \t\r\n]*(address|article|aside|blockquote|br|caption|center|dd|details|dialog|div|dl|dt"
    r"|fieldset|figcaption|figure|footer|form|h[1-6]|header|hgroup|hr|legend|li|main|menu|nav|ol|p|pre"
    r"|section|summary|table|tbody|td|tfoot|th|thead|tr|ul)\b([^>\"']|\"[^\"]*\"|'[^']*')*>"
)
# Postiz's own converter (stripHtmlValidation, read from the running
# container): an opening tag matching its regexes <p[^>]*> (so also <pre>,
# <param>...), <li.*?> or <ul> becomes a line break; striptags removes every
# other tag with no separator (<br> included). On HTML platforms only
# p/li/ul/h1-h3 survive as blocks. This is the text that is actually published.
POSTIZ_BREAK = r"<(p|li|ul|h[1-3])([^>\"']|\"[^\"]*\"|'[^']*')*>"
# How a tag affects the words around it is decided by whoever renders it, so
# the check covers every reading a real consumer uses and reports a violation
# found in ANY of them (claims_contract.check_text; install.sql violations):
#   "join"  - every tag removed with no separator: exactly what Postiz sends to
#             plain-text platforms (striptags; deploy/claimgate/README.md);
#   "postiz" - exactly what Postiz publishes: POSTIZ_BREAK separates, every
#             other tag joins ("P<br>ro<p>includes" reads "Pro includes");
#   "html"  - default browser display: BLOCK_TAG separates, inline tags join
#             ("P<b>ro</b><br>includes" reads "Pro includes");
#   "space" - every tag separates.
# CSS is out of scope: Postiz strips tags and styles before publishing, so no
# platform ever renders author CSS.
TAG_READINGS = ("join", "postiz", "html", "space")


def normalize(text: str, reading: str = "join") -> str:
    """The text BOTH engines check (install.sql: claimgate.normalize(body, reading)).

    1. tags handled per ``reading`` (see TAG_READINGS);
    2. entities decoded once, per the explicit spec above;
    3. ZERO_WIDTH deleted, SPACE_LIKE -> space;
    4. ASCII whitespace runs -> one space; trimmed of ASCII spaces only (the
       same thing Postgres btrim() does).
    """
    if reading not in TAG_READINGS:
        raise ValueError(f"unknown tag reading {reading!r}")
    text = text or ""
    if reading == "space":
        text = re.sub(TAG, " ", text)
    else:
        if reading == "html":
            text = re.sub(BLOCK_TAG, " ", text, flags=re.IGNORECASE)
        elif reading == "postiz":
            text = re.sub(POSTIZ_BREAK, " ", text, flags=re.IGNORECASE)
        text = re.sub(TAG, "", text)
    text = _ENTITY.sub(_decode_entity, text).translate(_ZW_TABLE)
    return re.sub(r"[ \t\r\n\f\v]+", " ", text).strip(" ")
