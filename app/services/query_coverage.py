"""Query-token coverage for rows that match no phrase tier (fed1006).

``federation_relevance`` ranks by WHOLE-phrase position (slug > title > prose).
A multi-word query leaves most rows in ``NO_MATCH_TIER``; there, this module
measures how much of the query a row covers, word by word.

Rules (each one exists because a review reproduced the failure it prevents):

- Words are Unicode-aware (``[\\W_]+`` split) and folded (NFKD, combining
  marks dropped, casefold) on both sides: Polish "żółć" stays one word. A token with CJK/kana/hangul characters matches as a SUBSTRING,
  because those scripts do not separate words with spaces.
- Matching is on word boundaries, never raw substrings: ``ai`` must not match
  inside ``email``.
- Word forms match through a small suffix stemmer (-s, -es after s/x/z/ch/sh,
  -ing, -ed, -er, -ers; stem of 4+ characters, so news != new, notes != not): ``convert`` ~ ``converting`` ~ ``converter``,
  ``image`` ~ ``images``. There is NO prefix rule: ``test`` does not match
  ``testament`` and ``react`` does not match ``reaction``.
- A token with a digit, or of 3 characters or fewer, matches only an equal
  word: ``ste100`` is not ``ste1000``; ``pdf`` is not ``pdfx``.
- Grammatical stopwords (a, the, to, of …) carry no weight. Words every skill
  query carries ("tool", "skill", "agent", "plugin", "find") carry HALF weight:
  they still separate "plugin memory" from "memory", but cannot outweigh the
  subject word. If every token is a stopword, all tokens count.
- The primary measure is the weighted share of tokens a row contains anywhere;
  the secondary measure is the weighted share found in the slug/title. A row
  that covers both query words beats a row that covers one, wherever the hits
  are.
- At most ``MAX_TOKENS`` tokens are scored. A longer query keeps its first and
  last ``MAX_TOKENS // 2`` tokens, so a final subject ("... pdf") survives. A
  query longer than that can lose a middle term; this is the documented cap.
- Not handled on purpose: 3-letter plurals (pdf/pdfs). Any such rule also
  makes new == news.
"""

from __future__ import annotations

import re
import unicodedata

MAX_TOKENS = 12
_WORD_SPLIT = re.compile(r"[\W_]+")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_SUFFIXES = ("ers", "ing", "es", "ed", "er", "s")
_ES_AFTER = ("s", "x", "z", "ch", "sh")
_MIN_STEM = 4
STOPWORDS = frozenset(
    {
        "a", "an", "the", "to", "of", "for", "in", "on", "at", "by", "with", "and", "or", "into",
        "from", "as", "is", "it", "its", "my", "me", "i", "you", "your", "that", "this", "these",
        "how", "what", "which", "can", "do", "does", "some", "any", "want", "need",
        "的", "の", "了", "和", "与", "與",
    }
)  # fmt: skip
GENERIC = frozenset(
    {
        "find",
        "use",
        "using",
        "help",
        "tool",
        "tools",
        "skill",
        "skills",
        "agent",
        "agents",
        "plugin",
        "plugins",
    }
)
GENERIC_WEIGHT = 0.5


def fold(text: str | None) -> str:
    """Accent- and case-insensitive form for Latin/Greek/Cyrillic ('İstanbul' ==
    'istanbul', 'café' == 'cafe\u0301'): NFKD with combining marks dropped,
    then casefold. CJK, kana and Hangul are only NFC-composed and kept whole:
    decomposing them would split Hangul syllables into jamo and strip Japanese
    voicing marks (fed1006 R4)."""
    out: list[str] = []
    for ch in unicodedata.normalize("NFC", text or ""):
        if _CJK.match(ch):
            out.append(ch)
        else:
            out.extend(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
    return "".join(out).casefold()


_SCRIPT_RUN = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]+|[^\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]+"
)


def words(text: str | None) -> list[str]:
    """Folded words; a mixed CJK+Latin run ('日本語React開発') splits at each
    script boundary into '日本語', 'react', '開発' (fed1006 R5)."""
    out: list[str] = []
    for w in _WORD_SPLIT.split(fold(text)):
        if w:
            out.extend(_SCRIPT_RUN.findall(w) if _CJK.search(w) else [w])
    return out


def significant_tokens(query: str | None) -> list[str]:
    """Distinct weighted query words; at most MAX_TOKENS (first and last halves)."""
    all_tokens = list(dict.fromkeys(words(query)))
    tokens = [t for t in all_tokens if t not in STOPWORDS] or all_tokens
    if len(tokens) > MAX_TOKENS:
        half = MAX_TOKENS // 2
        tokens = tokens[:half] + tokens[-half:]  # the subject is usually first or last
    return tokens


def weight(token: str) -> float:
    return GENERIC_WEIGHT if token in GENERIC else 1.0


def _stems(word: str) -> set[str]:
    out = {word}
    for suffix in ("ing", "ed"):
        # A 3-letter root only through the silent-e form (coding ~ code,
        # making ~ make); the bare 3-letter stem is never added (news != new).
        if word.endswith(suffix) and len(word) - len(suffix) == 3:
            out.add(word[: -len(suffix)] + "e")
    for suffix in _SUFFIXES:
        stem = word[: -len(suffix)]
        if not word.endswith(suffix) or len(stem) < _MIN_STEM:
            continue
        if suffix == "es" and not stem.endswith(_ES_AFTER):
            continue
        out.add(stem)
        if suffix in ("ed", "ing", "er", "ers"):
            out.add(stem + "e")  # silent e: updated ~ update, sharing ~ share
    return out


def _hit(token: str, field_words: list[str], field_text: str) -> bool:
    if token in field_words:
        return True
    if any(c.isdigit() for c in token):
        return False  # identifiers and versions: exact only (ste100 != ste1000, 模型2 != 模型20)
    if _CJK.search(token):
        return token in field_text
    if len(token) <= 3:
        return False
    stems = _stems(token)
    return any(
        not stems.isdisjoint(_stems(w))
        for w in field_words
        if len(w) >= 3 and not any(c.isdigit() for c in w)
    )


def coverage(tokens: list[str], *, slug: str, title: str, description: str) -> tuple[float, float]:
    """(weighted share of tokens found anywhere, weighted share found in slug/title)."""
    total = sum(weight(t) for t in tokens)
    if not total:
        return 0.0, 0.0
    head_text = fold(f"{slug} {title}")
    body_text = fold(description)
    head_words, body_words = words(head_text), words(body_text)
    anywhere = head = 0.0
    for t in tokens:
        w = weight(t)
        if _hit(t, head_words, head_text):
            anywhere += w
            head += w
        elif _hit(t, body_words, body_text):
            anywhere += w
    return anywhere / total, head / total
