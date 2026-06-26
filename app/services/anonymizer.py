"""app/services/anonymizer.py

Regex-based anonymizer for skill content before it enters the public Menu or
Base Bundle. Strips personal tokens, infra references, paths, agent names
(optional), and non-allowlisted email addresses.

Usage:
    from app.services.anonymizer import anonymize, Finding
    cleaned_text, findings = anonymize(raw_text, user_facing=False)
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# ── Sensitive-token configuration ──────────────────────────────────────────
# The anonymizer redacts deployment-specific names, infra refs, home paths, and
# agent names from text. These lists are DEPLOYMENT-SPECIFIC and must NOT be
# hard-coded in the open-source tree (doing so would leak the very identifiers
# the scrubber exists to hide). They load from an optional JSON config:
#
#   config/anonymizer_tokens.json   (gitignored; copy from the .example file)
#
# Self-hosters populate it with their own names / hostnames. When the
# file is absent the scrubber still runs with safe, generic defaults (paths +
# email redaction always active; name/infra lists empty unless configured).
_DEFAULTS: dict[str, list[str]] = {
    "user_tokens": [],
    "infra_refs": [],
    "agent_names": [],
    "paths": ["/home/", "/Users/", "$HOME/"],
}


def _load_tokens() -> dict[str, list[str]]:
    cfg_path = os.environ.get(
        "WR_ANONYMIZER_CONFIG",
        str(Path(__file__).resolve().parent.parent.parent / "config" / "anonymizer_tokens.json"),
    )
    merged = {k: list(v) for k, v in _DEFAULTS.items()}
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            data = json.load(fh)
        for key in merged:
            if isinstance(data.get(key), list):
                # config replaces defaults for name/infra lists; paths extend
                if key == "paths":
                    merged[key] = list(dict.fromkeys(merged[key] + data[key]))
                else:
                    merged[key] = list(data[key])
    except FileNotFoundError:
        pass  # Rationale: config is optional; generic defaults are the public-safe baseline.
    except (json.JSONDecodeError, OSError):
        pass  # Rationale: a malformed/unreadable config must never break request handling.
    return merged


_TOKENS = _load_tokens()
ADAM_TOKENS: list[str] = _TOKENS["user_tokens"]
INFRA_REFS: list[str] = _TOKENS["infra_refs"]
_PATHS: list[str] = _TOKENS["paths"]
_AGENT_NAMES: list[str] = _TOKENS["agent_names"]

# Matches any email EXCEPT @example.com and @wisechef.ai (allowlisted)
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@(?!example\.com\b|wisechef\.ai\b)[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)


@dataclass
class Finding:
    """A single redaction finding."""

    category: str  # adam_token | infra_ref | path | agent_name | email
    original: str
    replacement: str
    start: int
    end: int


def anonymize(text: str, user_facing: bool = False) -> tuple[str, list[Finding]]:
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
        findings.append(
            Finding(
                category=category,
                original=original,
                replacement=replacement,
                start=start,
                end=end,
            )
        )
        result = result[:start] + replacement + result[end:]

    # Reverse findings back to left-to-right order
    findings.reverse()
    return result, findings
