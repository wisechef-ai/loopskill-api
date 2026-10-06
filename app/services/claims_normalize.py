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


# What Postiz publishes is stripHtmlValidation(): parse5 (HTML5) parse +
# serialize, THEN striptags. So a "<" opens a tag only where HTML5 says it
# does ("<" + ASCII letter, "/", "!" or "?"); "We <3 you" keeps its "<3" as
# text. Rather than re-implement HTML5 tree construction (tables, textarea,
# comments, foster parenting...), marketing markup is ALLOWLISTED:
#   * TAGLIKE finds every token HTML5 would treat as markup, up to the first
#     ">" (or the end of the text);
#   * a token that is not a plain ALLOWED_TAG (the vocabulary producers and
#     the Postiz editor emit: production uses only <p> and <br>) is itself a
#     violation, "unsupported-markup": comments, <! / <? constructs, unknown
#     elements, single-quoted or ">"-containing attributes, unterminated tags.
# For text that passes the allowlist, the "join" reading equals what Postiz
# publishes, byte for byte after whitespace normalisation: property-tested
# against Postiz's real converter (parse5 6.0.1 + striptags 3.2.0, copied from
# the running container) in tests/fixtures/postiz_pipeline.json.
TAGLIKE = r"<[A-Za-z/!?][^>]*>?"
# Attributes only as <a href="...">: any other attribute (data-mention-id is
# rewritten per platform by Postiz's convertMention) is unsupported markup.
ALLOWED_TAG = r'^(</?(p|strong|b|em|i|u|s|ul|ol|li|h[1-3]|span)>|<br ?/?>|<a href="[^"<>]*">|</a>)$'
# A whole link, matched the way Postiz's /<a.*?href="(.*?)".*?>(.*?)<\/a>/g
# matches it (content runs to the FIRST "</a>"): \1 = href, \2 = text.


def upto(tag: str) -> str:
    """Portable "any text up to the first </tag>" (no lookahead): every "<"
    run must be followed by something that cannot start "</tag>"."""
    stop = ["[^/<]"] + ["/" + tag[:k] + f"[^{tag[k]}]" for k in range(len(tag))] + [f"/{tag}[^>]"]
    return r"((([^<]|<+(" + "|".join(stop) + r"))*)<*)"


LINK = r'<a href="([^"<>]*)">' + upto("a") + "</a>"
# Readings decide what an (allowed) tag becomes. A violation found in ANY
# reading counts (claims_contract.check_text; install.sql violations):
#   "join"   - nothing: Postiz's plain-text output (striptags);
#   "postiz" - a separator for opening tags Postiz's own regexes turn into a
#              line break (<p[^>]*>, <li.*?>, <ul>; h1-h3 kept on HTML
#              platforms), nothing for the rest (<br> included);
#   "html"   - a separator for default block / line-break elements, nothing for
#              inline ones ("P<b>ro</b><br>includes" reads "Pro includes");
#   "space"  - a separator for every tag.
# CSS is out of scope: Postiz strips tags and styles before publishing.
POSTIZ_TAG = r"<(p|li|ul|h[1-3])[^>]*>?"
BLOCK_TAG = (
    r"</?(address|article|aside|blockquote|br|caption|center|dd|details|dialog|div|dl|dt"
    r"|fieldset|figcaption|figure|footer|form|h[1-6]|header|hgroup|hr|legend|li|main|menu|nav|ol|p|pre"
    r"|section|summary|table|tbody|td|tfoot|th|thead|tr|ul)\b[^>]*>?"
)
#   "link"   - Postiz "normal" + replaceBold, emulated step by step
#              (postiz_bold): <p> -> line break, a link -> its href, <li>
#              content loses its newlines ("Pro\n+" -> "Pro+");
#   "markdown" - Postiz "markdown", emulated step by step (postiz_markdown).
# Both are property-tested against Postiz's real converter
# (tests/fixtures/postiz_pipeline.json). Postiz's regexes run on parse5
# output, which lowercases tags, so ours match case-insensitively.
TAG_READINGS = ("join", "postiz", "html", "space", "link", "markdown")
LI_BLOCK = "<li>" + upto("li") + "</li>"
P_BLOCK = "<p>" + upto("p") + "</p>"


def _wrap(tag: str, before: str, after: str, h: str) -> str:
    """Postiz's <tag>(content)</tag> -> <tag>{before}content{after}</tag>."""
    return re.sub(
        f"<{tag}>{upto(tag)}</{tag}>", lambda m: f"<{tag}>{before}{m.group(1)}{after}</{tag}>", h, flags=re.I
    )


def postiz_bold(h: str) -> str:
    """stripHtmlValidation('normal', h, replaceBold=True), minus convertToAscii."""
    h = re.sub(r"^<p>", "", h, flags=re.I)
    h = re.sub(r"<p>", "\n", h, flags=re.I)
    h = re.sub(r"</p>", "", h, flags=re.I)
    h = re.sub(LINK, lambda m: m.group(1), h, flags=re.I)
    h = re.sub(r"<ul>", "\n<ul>", h, count=1, flags=re.I)
    h = re.sub(r"</ul>\n", "</ul>", h, count=1, flags=re.I)
    h = re.sub(LI_BLOCK, lambda m: "- " + re.sub(r"[\r\n]", "", m.group(1)) + "\n", h, flags=re.I)
    return re.sub(TAGLIKE, "", h)


def postiz_markdown(h: str) -> str:
    """stripHtmlValidation('markdown', h)."""
    for n in (1, 2, 3):
        h = _wrap(f"h{n}", "#" * n + " ", "", h)
        h = re.sub(f"</h{n}>", f"</h{n}>\n", h, flags=re.I)
    h = _wrap("u", "__", "__", h)
    h = _wrap("strong", "**", "**", h)
    h = re.sub(LI_BLOCK, lambda m: "<li>- " + re.sub(r"[\r\n]", "", m.group(1)) + "</li>", h, flags=re.I)
    h = re.sub(P_BLOCK, lambda m: "<p>" + m.group(1) + "</p>\n", h, flags=re.I)
    h = re.sub(LINK, lambda m: f"[{m.group(2)}]({m.group(1)})", h, flags=re.I)
    return re.sub(TAGLIKE, "", h)


BLOCK = ("p", "ul", "ol", "h1", "h2", "h3")


def markup_structure_error(html: str) -> str:
    """'' when the allowed tags form a tree HTML5 keeps exactly as written.

    parse5 auto-closes / re-parents anything else ("<h2>x" gains "</h2>", an
    unclosed <a> swallows the rest), which no reading can predict. So tags must
    be balanced and nested by these rules (install.sql: claimgate.markup_structure):
    p/ul/ol/h1-h3 only at top level or in <li>; <li> only in <ul>/<ol>; inline
    elements never in <ul>/<ol> and <a> never in <a>; <br> is void.
    """
    stack: list[str] = []
    for tok in re.findall(TAGLIKE, html or ""):
        m = re.match(r"<(/?)([a-z0-9]+)", tok.lower())
        if not m or m.group(2) == "br":
            continue
        closing, name = m.group(1) == "/", m.group(2)
        top = stack[-1] if stack else ""
        if closing:
            if top != name:
                return f"</{name}> closes <{top or 'nothing'}>"
            stack.pop()
            continue
        if name in BLOCK and top not in ("", "li"):
            return f"<{name}> inside <{top}>"
        if name == "li" and top not in ("ul", "ol"):
            return f"<li> inside <{top or 'top level'}>"
        if name not in BLOCK and name != "li" and (top in ("ul", "ol") or (name == "a" and "a" in stack)):
            return f"<{name}> inside <{top}>"
        stack.append(name)
    return f"<{stack[-1]}> is never closed" if stack else ""


def unsupported_markup(html: str) -> list[str]:
    """Markup tokens outside the allowlist, plus a structure error if any
    (install.sql: claimgate.markup_hits)."""
    bad = [t for t in re.findall(TAGLIKE, html or "") if not re.match(ALLOWED_TAG, t, re.IGNORECASE)]
    err = "" if bad else markup_structure_error(html)
    return bad + ([err] if err else [])


def markup_violations(html: str) -> list[dict]:
    """One violation per distinct markup token outside the allowlist."""
    return [
        {
            "kind": "markup",
            "rule_id": "unsupported-markup",
            "match": tok[:80],
            "excerpt": tok[:80],
            "reason": "markup outside the marketing allowlist (ALLOWED_TAG): its published text "
            "cannot be predicted, so it is rejected",
            "replacement": 'plain text, or <p>/<br>/<strong>/<em>/<a href="...">',
        }
        for tok in dict.fromkeys(unsupported_markup(html))
    ]


def strip_tags(html: str, reading: str = "join") -> str:
    """Remove markup per ``reading`` (install.sql: claimgate.strip_tags, same regexes)."""
    if reading not in TAG_READINGS:
        raise ValueError(f"unknown tag reading {reading!r}")
    if reading == "link":
        return postiz_bold(html)
    if reading == "markdown":
        return postiz_markdown(html)
    if reading == "space":
        return re.sub(TAGLIKE, " ", html)
    if reading == "postiz":
        html = re.sub(POSTIZ_TAG, " ", html, flags=re.IGNORECASE)
    elif reading == "html":
        html = re.sub(BLOCK_TAG, " ", html, flags=re.IGNORECASE)
    return re.sub(TAGLIKE, "", html)


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
    text = strip_tags(text or "", reading)
    text = _ENTITY.sub(_decode_entity, text).translate(_ZW_TABLE)
    return re.sub(r"[ \t\r\n\f\v]+", " ", text).strip(" ")
