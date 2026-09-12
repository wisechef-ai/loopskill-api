"""Normalize federated skill titles before they enter the hub index."""

from __future__ import annotations

import re
import unicodedata

MAX_FEDERATED_TITLE_LENGTH = 120
_PROMPT_LEAD_RE = re.compile(r"^\s*\d+[.)]\s")
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_federated_title(title: object, fallback: object = "") -> str:
    """Return a compact display title, using an identifier for prompt blobs.

    Hub ``name`` values occasionally contain the source skill's full
    instructions.  Identifiers are stable, human-readable enough fallbacks;
    ordinary names are retained and bounded for the database/API contract.
    """
    value = _clean_text(title)
    fallback_value = _clean_text(fallback)
    if fallback_value and _looks_like_prompt(value):
        value = fallback_value
    return value[:MAX_FEDERATED_TITLE_LENGTH].rstrip()


def _clean_text(value: object) -> str:
    text = "" if value is None else str(value)
    cleaned = "".join(
        " " if ch.isspace() else ch
        for ch in text
        if not unicodedata.category(ch).startswith("C") or ch.isspace()
    )
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def _looks_like_prompt(value: str) -> bool:
    return len(value) > MAX_FEDERATED_TITLE_LENGTH or bool(_PROMPT_LEAD_RE.match(value))
