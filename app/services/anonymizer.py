"""app/services/anonymizer.py

Regex-based anonymizer for skill content before it enters the public Menu or
Base Cookbook. Strips personal tokens, infra references, paths, agent names
(optional), and non-allowlisted email addresses.

Usage:
    from app.services.anonymizer import anonymize, Finding
    cleaned_text, findings = anonymize(raw_text, user_facing=False)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Tuple


ADAM_TOKENS: list[str] = ["Adam", "Bombilla", "Marco", "Karol", "Olek", "Mariusz"]

INFRA_REFS: list[str] = [
    "wisechef-agents", "wisechef-hq", "paperclip", "obsidian-vault",
    "adam-xps", "wisevision",
]

_PATHS: list[str] = ["/home/adam/", "/Users/adam/", "$HOME/"]

_AGENT_NAMES: list[str] = ["Tori", "Wise", "Chef"]

# Matches any email EXCEPT @example.com and @wisechef.ai (allowlisted)
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@(?!example\.com\b|wisechef\.ai\b)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)


@dataclass
class Finding:
    """A single redaction finding."""
    category: str       # adam_token | infra_ref | path | agent_name | email
    original: str
    replacement: str
    start: int
    end: int


def anonymize(text: str, user_facing: bool = False) -> Tuple[str, List[Finding]]:
    """Replace sensitive tokens in *text* and return (cleaned, findings).

    Rules applied in order (positions shift as replacements happen, so we
    collect all match positions first, then apply right-to-left):

    1. ADAM_TOKENS  → <USER>   (exact word boundary, case-sensitive)
    2. INFRA_REFS   → <INFRA>  (substring match, case-sensitive)
    3. Paths        → <HOME>   (prefix match)
    4. Emails       → <EMAIL>  (regex, excludes wisechef.ai + example.com)
    5. AGENT_NAMES  → <AGENT>  (only when user_facing=True)
    """
    # Collect all (start, end, original, replacement, category) tuples
    spans: list[tuple[int, int, str, str, str]] = []

    # 1. ADAM_TOKENS — whole-word, case-sensitive
    for token in ADAM_TOKENS:
        pattern = re.compile(r"\b" + re.escape(token) + r"\b")
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end(), m.group(), "<USER>", "adam_token"))

    # 2. INFRA_REFS — substring
    for ref in INFRA_REFS:
        pattern = re.compile(re.escape(ref))
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end(), m.group(), "<INFRA>", "infra_ref"))

    # 3. Paths — simple prefix substring
    for path in _PATHS:
        pattern = re.compile(re.escape(path))
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end(), m.group(), "<HOME>", "path"))

    # 4. Emails
    for m in _EMAIL_RE.finditer(text):
        spans.append((m.start(), m.end(), m.group(), "<EMAIL>", "email"))

    # 5. Agent names — only in user-facing context
    if user_facing:
        for name in _AGENT_NAMES:
            pattern = re.compile(r"\b" + re.escape(name) + r"\b")
            for m in pattern.finditer(text):
                spans.append((m.start(), m.end(), m.group(), "<AGENT>", "agent_name"))

    if not spans:
        return text, []

    # Remove overlapping spans (keep first-found; sort by start then by length desc)
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    deduped: list[tuple[int, int, str, str, str]] = []
    last_end = -1
    for span in spans:
        if span[0] >= last_end:
            deduped.append(span)
            last_end = span[1]

    # Apply replacements right-to-left so earlier positions stay valid
    findings: list[Finding] = []
    result = text
    for start, end, original, replacement, category in reversed(deduped):
        findings.append(Finding(
            category=category,
            original=original,
            replacement=replacement,
            start=start,
            end=end,
        ))
        result = result[:start] + replacement + result[end:]

    # Reverse findings back to left-to-right order
    findings.reverse()
    return result, findings
