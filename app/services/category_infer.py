"""category_infer.py — deterministic category inference for uncategorized skills.

atomic-habits 2026-07-16 rank-1: skill_routes.py L133-146 already widened the
literal search pass to match category+readme, but 48/53 catalog skills carry
category=NULL — so the widened pass still has nothing to match against for
the vast majority of the catalog (verified live: GET /api/stats
by_category=[uncategorized:48, data:2, ops:2, automation:1]).

This module is the classifier used by the one-time backfill migration
(alembic/versions/f8ade9aa1b68_category_backfill_null.py) AND is importable
directly so future skill-creation paths (publisher_routes.py) can call it as
a category-suggestion default instead of leaving new skills uncategorized
again.

Keyword buckets are authored directly from docs/taxonomy.md's canonical-10
list and its legacy->canonical mapping table — same SSOT, no invented
vocabulary.

Matching discipline (v3 — hardened 2026-07-16 by the test suite catching two
classes of real bug in v1/v2):
  - Single-word keywords (no hyphen, no space) match with FULL word
    boundaries on both sides (\\bword\\b), so "bot" doesn't fire inside
    "bottlenecks", "ide" doesn't fire inside "ideas", "loop" doesn't fire
    inside "loopskill". Plural/inflected forms that matter are listed
    explicitly (e.g. both "offer" and "offers", both "scrape" and
    "scraper") rather than relying on fuzzy stemming.
  - Multi-word / hyphenated compound keywords (e.g. "client-report",
    "code-review", "hub-search") are normalized (hyphens -> spaces) and
    matched with a LEFT word-boundary only, tolerating the phrase being a
    grammatical prefix of a longer inflected word in the haystack — this is
    what lets "client-report" correctly match the slug "client-reporter"
    and "code-review" match "code-reviewer" without needing every
    inflection spelled out.
  - Matching is a single FLAT pool across all buckets, sorted by
    (normalized) keyword length descending, so more specific compound terms
    win over generic single words automatically (e.g. "hub-search"
    outranks the bare "search").

Anything matching nothing falls back to "productivity" (taxonomy.md's
documented lowest-risk fallback bucket) — never leaves category NULL and
never invents a new bucket.
"""
from __future__ import annotations

import re

CANONICAL_CATEGORIES = {
    "research", "dev-tools", "agency", "marketing", "content",
    "automation", "code-review", "productivity", "data", "ops",
}

# bucket -> keywords. Authored from docs/taxonomy.md's canonical-10 +
# legacy-mapping table. Bucket grouping order is for readability only —
# actual match priority is decided globally by keyword length (see
# _flat_rules()), not by bucket order.
_KEYWORD_RULES: dict[str, tuple[str, ...]] = {
    "code-review": (
        "code-review", "code-reviewer", "code review", "lint",
        "security", "audit", "vuln", "static analysis", "code-quality",
    ),
    "agency": (
        "client-report", "client-reporting", "client-reporter",
        "consult", "proposal", "agency", "agencies", "scoping",
        "deliverable",
    ),
    "data": (
        "scrape", "scraper", "scraping", "data-extraction", "etl",
        "ml pipeline", "machine-learning", "analytics", "dataset",
        "data pipeline", "crawler",
    ),
    "marketing": (
        "marketing", "seo", "ads", "advertis", "growth", "campaign",
        "lead-gen", "leadgen", "offer", "offers", "conversion", "funnel",
        "outreach", "newsletter",
    ),
    "content": (
        "copywrit", "creative", "video", "image", "carousel", "reel",
        "illustrat", "design", "art",
    ),
    "ops": (
        "devops", "infra", "infrastructure", "platform", "monitor",
        "deploy", "cron", "watchdog", "server", "kubernetes", "docker",
        "ci/cd", "github-actions",
    ),
    "research": (
        "research", "discovery", "knowledge", "wiki", "memory",
        "recall", "brain", "gbrain", "cognee", "search engine",
        "web search",
    ),
    "automation": (
        "automation", "workflow", "bot", "scheduler", "loop", "agent",
        "autonomous", "orchestrat",
    ),
    "dev-tools": (
        "dev-tools", "development", "coding", "cli", "ide", "api",
        "sdk", "developer", "github", "git ", "hub-search", "search",
    ),
    "productivity": (
        "communication", "email", "tutorial", "general", "utility",
        "productivity", "goal", "plan-for", "mentor", "algorithm",
        "notes", "calendar",
    ),
}


def _normalize(term: str) -> str:
    return term.replace("-", " ").replace("_", " ").strip()


def _flat_rules() -> list[tuple[str, str, bool]]:
    """(normalized_keyword, bucket, is_phrase) triples, longest first.

    is_phrase=True for any keyword that originally contained a hyphen or
    space (compound term) — these get left-boundary-only (prefix-tolerant)
    matching. is_phrase=False for plain single-word keywords, which get
    strict word-boundary-on-both-sides matching.
    """
    pairs: list[tuple[str, str, bool]] = []
    for bucket, keywords in _KEYWORD_RULES.items():
        for kw in keywords:
            is_phrase = ("-" in kw) or (" " in kw)
            pairs.append((_normalize(kw), bucket, is_phrase))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


_FLAT_RULES = _flat_rules()


def _keyword_present(haystack: str, keyword_norm: str, is_phrase: bool) -> bool:
    escaped = re.escape(keyword_norm)
    if is_phrase:
        # Left boundary only — tolerates the phrase being a grammatical
        # prefix of a longer word/phrase in the haystack (e.g. "client
        # report" matching "client reporter").
        return re.search(r"\b" + escaped, haystack) is not None
    # Strict word boundary both sides — avoids short generic tokens
    # matching inside unrelated longer words (e.g. "bot" in "bottleneck").
    return re.search(r"\b" + escaped + r"\b", haystack) is not None


def classify_category(
    title: str | None = None,
    description: str | None = None,
    slug: str | None = None,
    readme: str | None = None,
) -> str:
    """Return one of CANONICAL_CATEGORIES inferred from skill text fields.

    Concatenates the given fields, normalizing hyphens to spaces so
    hyphenated slugs (e.g. "client-reporter") match phrase keywords
    (e.g. "client-report" -> "client report") the same way a plain-text
    title or description would. Falls back to "productivity" per
    docs/taxonomy.md's documented fallback rule — never returns None and
    never invents a bucket outside CANONICAL_CATEGORIES.
    """
    raw_parts = [p for p in (title, slug, description, readme) if p]
    if not raw_parts:
        return "productivity"

    haystack = " ".join(f" {_normalize(p)} " for p in raw_parts).lower()

    if not haystack.strip():
        return "productivity"

    for keyword_norm, bucket, is_phrase in _FLAT_RULES:
        if _keyword_present(haystack, keyword_norm, is_phrase):
            return bucket

    return "productivity"
