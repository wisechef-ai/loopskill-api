"""Query-token coverage for rows that match no phrase tier (fed1006).

``federation_relevance`` ranks by WHOLE-phrase position (slug > title > prose).
A multi-word query leaves most rows in ``NO_MATCH_TIER``; there, this module
measures how much of the query a row covers, word by word.

Matching is on word boundaries, never raw substrings: ``ai`` must not match
inside ``email``, ``go`` inside ``django`` (fed1006 R1). Rules:

- Query and fields are split into lower-case words on any non-alphanumeric.
- A token of 3 characters or fewer matches only an equal word (``ai``, ``go``,
  ``pdf`` stay meaningful when the user types them).
- A longer token also matches a word it is a prefix of, or a word of 4+
  characters that is a prefix of it (``convert`` ~ ``converter``,
  ``images`` ~ ``image``).
- Low-signal words (articles, prepositions, and the words every skill query
  carries: "find", "tool", "skill", "agent") do not count, unless the query has
  nothing else.
- A slug/title hit counts 1, a description-only hit 0.5; the sum is divided by
  the number of significant tokens. At most ``MAX_TOKENS`` significant tokens
  are scored, so per-row cost stays bounded without dropping a query's tail to
  filler words.
"""

from __future__ import annotations

import re

MAX_TOKENS = 12
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
LOW_SIGNAL = frozenset(
    {
        "a", "an", "the", "to", "of", "for", "in", "on", "at", "by", "with", "and", "or", "into",
        "from", "as", "is", "it", "its", "my", "me", "i", "you", "your", "that", "this", "these",
        "how", "what", "which", "can", "do", "does", "some", "any", "best", "want", "need",
        "find", "use", "using", "help", "helps", "tool", "tools", "skill", "skills", "agent",
        "agents", "plugin", "plugins",
    }
)  # fmt: skip


def words(text: str | None) -> list[str]:
    return [w for w in _WORD_SPLIT.split((text or "").lower()) if w]


def significant_tokens(query: str | None) -> list[str]:
    """Distinct query words that carry signal, in query order, capped."""
    all_tokens = list(dict.fromkeys(words(query)))
    sig = [t for t in all_tokens if t not in LOW_SIGNAL]
    return (sig or all_tokens)[:MAX_TOKENS]


def _hit(token: str, field_words: set[str]) -> bool:
    if token in field_words:
        return True
    if len(token) <= 3:
        return False
    return any(w.startswith(token) or (len(w) >= 4 and token.startswith(w)) for w in field_words)


def coverage(tokens: list[str], *, slug: str, title: str, description: str) -> float:
    """Share of ``tokens`` a row covers: head (slug/title) hit 1, prose 0.5."""
    if not tokens:
        return 0.0
    head = set(words(slug)) | set(words(title))
    body = set(words(description))
    score = 0.0
    for t in tokens:
        if _hit(t, head):
            score += 1.0
        elif _hit(t, body):
            score += 0.5
    return score / len(tokens)
