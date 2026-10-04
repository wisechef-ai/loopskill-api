"""Query-token coverage for rows that match no phrase tier (fed1006).

``federation_relevance`` ranks by WHOLE-phrase position (slug > title > prose).
A multi-word query leaves most rows in ``NO_MATCH_TIER``; there, this module
measures how much of the query a row covers, word by word.

Rules (each one exists because a review reproduced the failure it prevents):

- Words are Unicode-aware (``[\\W_]+`` split, casefolded): Polish "żółć" stays
  one word. A token with CJK/kana/hangul characters matches as a SUBSTRING,
  because those scripts do not separate words with spaces.
- Matching is on word boundaries, never raw substrings: ``ai`` must not match
  inside ``email``.
- Word forms match through a small suffix stemmer (-s, -es, -ing, -ed, -er,
  -ers; stem of 3+ characters): ``convert`` ~ ``converting`` ~ ``converter``,
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
- At most ``MAX_TOKENS`` tokens are scored. A longer query keeps its longest
  (most specific) tokens, so a late discriminator is not cut off by filler.
"""

from __future__ import annotations

import re

MAX_TOKENS = 12
_WORD_SPLIT = re.compile(r"[\W_]+")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_SUFFIXES = ("ers", "ing", "es", "ed", "er", "s")
STOPWORDS = frozenset(
    {
        "a", "an", "the", "to", "of", "for", "in", "on", "at", "by", "with", "and", "or", "into",
        "from", "as", "is", "it", "its", "my", "me", "i", "you", "your", "that", "this", "these",
        "how", "what", "which", "can", "do", "does", "some", "any", "want", "need",
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


def words(text: str | None) -> list[str]:
    return [w for w in _WORD_SPLIT.split((text or "").casefold()) if w]


def significant_tokens(query: str | None) -> list[str]:
    """Distinct weighted query words; at most MAX_TOKENS, the longest kept."""
    all_tokens = list(dict.fromkeys(words(query)))
    tokens = [t for t in all_tokens if t not in STOPWORDS] or all_tokens
    if len(tokens) > MAX_TOKENS:
        keep = set(sorted(tokens, key=len, reverse=True)[:MAX_TOKENS])
        tokens = [t for t in tokens if t in keep][:MAX_TOKENS]
    return tokens


def weight(token: str) -> float:
    return GENERIC_WEIGHT if token in GENERIC else 1.0


def _stems(word: str) -> set[str]:
    out = {word}
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            out.add(word[: -len(suffix)])
    return out


def _hit(token: str, field_words: list[str], field_text: str) -> bool:
    if _CJK.search(token):
        return token in field_text
    if token in field_words:
        return True
    if len(token) <= 3 or any(c.isdigit() for c in token):
        return False
    stems = _stems(token)
    return any(
        not stems.isdisjoint(_stems(w)) for w in field_words if len(w) >= 3 and not any(c.isdigit() for c in w)
    )


def coverage(tokens: list[str], *, slug: str, title: str, description: str) -> tuple[float, float]:
    """(weighted share of tokens found anywhere, weighted share found in slug/title)."""
    total = sum(weight(t) for t in tokens)
    if not total:
        return 0.0, 0.0
    head_text = f"{slug} {title}".casefold()
    body_text = (description or "").casefold()
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
