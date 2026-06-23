"""scripts/voc_issues_pulse.py — topshelf_2605/H.3

GitHub issues pulse — surfaces the top 3 recurring themes from recent
issues on wisechef-ai/recipes-api using keyword frequency clustering.

No LLM required: uses collections.Counter on labels + title words.
Requires the GitHub CLI (``gh``) to be installed and authenticated.

Usage:
    python scripts/voc_issues_pulse.py
    python scripts/voc_issues_pulse.py --days 14
    python scripts/voc_issues_pulse.py --repo wisechef-ai/recipes-api --days 7
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import date, timedelta, timezone
from datetime import datetime as dt

# Words that carry no signal for clustering — filtered out before counting.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "can", "not", "this",
    "that", "it", "its", "i", "we", "you", "he", "she", "they", "my",
    "our", "your", "their", "when", "where", "how", "what", "which",
    "there", "then", "than", "so", "if", "no", "all", "any", "more",
    "also", "into", "up", "out", "about", "after", "before", "during",
    "get", "got", "add", "added", "fix", "fixed", "update", "updated",
    "new", "old", "use", "using", "via", "just", "make", "made",
}


def _gh_issues(repo: str, since: date) -> list[dict]:
    """Fetch issues updated since ``since`` via the GitHub CLI."""
    since_iso = dt(since.year, since.month, since.day, tzinfo=timezone.utc).isoformat()
    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "all",
        "--limit", "200",
        "--json", "title,body,labels,createdAt",
        "--search", f"created:>={since_iso[:10]}",
    ]
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("ERROR: 'gh' CLI not found. Install it from https://cli.github.com/", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: gh CLI failed:\n{exc.stderr}", file=sys.stderr)
        sys.exit(1)

    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Could not parse gh output: {exc}", file=sys.stderr)
        sys.exit(1)

    return issues  # type: ignore[return-value]


def _tokenise(text: str) -> list[str]:
    """Lower-case, split on non-alphanumeric, filter stopwords and short tokens."""
    tokens = re.findall(r"[a-z0-9][a-z0-9_-]*", text.lower())
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 2]


def _cluster_themes(issues: list[dict], top_n: int = 3) -> list[tuple[str, int, list[str]]]:
    """Return top ``top_n`` themes as (term, count, example_titles) tuples.

    Scoring: label names count 3× (structured signal); title words count 1×.
    """
    term_counter: Counter[str] = Counter()
    term_to_titles: dict[str, list[str]] = {}

    for issue in issues:
        title = issue.get("title") or ""
        labels = [lbl.get("name", "") for lbl in (issue.get("labels") or [])]

        # Label names carry strong signal — weight 3×.
        for label in labels:
            for tok in _tokenise(label):
                term_counter[tok] += 3
                term_to_titles.setdefault(tok, [])
                if title and title not in term_to_titles[tok]:
                    term_to_titles[tok].append(title)

        # Title words — weight 1×.
        for tok in _tokenise(title):
            term_counter[tok] += 1
            term_to_titles.setdefault(tok, [])
            if title and title not in term_to_titles[tok]:
                term_to_titles[tok].append(title)

    # Build result: include 1-3 example titles per theme.
    themes = []
    for term, count in term_counter.most_common(top_n):
        examples = term_to_titles.get(term, [])[:3]
        themes.append((term, count, examples))

    return themes


def main(repo: str = "wisechef-ai/recipes-api", days: int = 7) -> None:
    """Fetch issues, cluster by keyword, print top 3 themes."""
    since = date.today() - timedelta(days=days)

    print(f"Fetching issues from {repo} (last {days} days, since {since}) …")
    issues = _gh_issues(repo=repo, since=since)
    print(f"Fetched {len(issues)} issue(s).\n")

    if not issues:
        print("No issues found in the specified window.")
        return

    themes = _cluster_themes(issues, top_n=3)

    if not themes:
        print("No themes identified (no label or title tokens after stopword filter).")
        return

    print(f"=== GitHub Issues Pulse — top 3 themes (last {days}d) ===")
    print(f"Repo: {repo}  |  Window: {since} → {date.today()}")
    print()

    for rank, (term, score, examples) in enumerate(themes, start=1):
        print(f"Theme #{rank}: [{term}]  (weighted score: {score})")
        for ex in examples:
            print(f"    • {ex}")
        print()

    print("Note: score = 3×label_hits + 1×title_word_hits (no LLM, pure frequency).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GitHub issues pulse — top 3 themes.")
    parser.add_argument(
        "--repo",
        default="wisechef-ai/recipes-api",
        help="GitHub repo slug (default: wisechef-ai/recipes-api)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Look-back window in days (default: 7)",
    )
    args = parser.parse_args()
    main(repo=args.repo, days=args.days)
