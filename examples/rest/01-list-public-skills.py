#!/usr/bin/env python3
"""Example 01 — List public skills via GET /api/skills/search.

Auth: x-api-key header (rec_* key)
Env:  RECIPES_API_KEY   — your API key (required)
      RECIPES_BASE_URL  — override base URL (default: https://recipes.wisechef.ai)

Usage:
    RECIPES_API_KEY=rec_xxx python examples/rest/01-list-public-skills.py
    RECIPES_API_KEY=rec_xxx python examples/rest/01-list-public-skills.py --query scraping
    RECIPES_API_KEY=rec_xxx python examples/rest/01-list-public-skills.py --category marketing
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("RECIPES_BASE_URL", "https://recipes.wisechef.ai").rstrip("/")


def build_url(path: str, params: dict[str, str] | None = None) -> str:
    url = BASE_URL + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v})
    return url


def list_skills(api_key: str, query: str = "", category: str = "") -> None:
    params: dict[str, str] = {}
    if query:
        params["q"] = query
    if category:
        params["category"] = category

    url = build_url("/api/skills/search", params)
    req = urllib.request.Request(
        url,
        headers={"x-api-key": api_key, "Accept": "application/json"},
        method="GET",
    )

    print(f"GET {url}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.reason}", file=sys.stderr)
        body = exc.read().decode()
        print(body, file=sys.stderr)
        sys.exit(1)

    data = json.loads(body)
    skills = data if isinstance(data, list) else data.get("results", data.get("skills", []))
    print(f"\nFound {len(skills)} skill(s):\n")
    for skill in skills:
        slug = skill.get("slug", "?")
        name = skill.get("name", slug)
        tier = skill.get("tier", "free")
        category_out = skill.get("category", "")
        description = (skill.get("description") or "")[:80]
        print(f"  [{tier:8s}] {slug:40s} — {name}")
        if category_out:
            print(f"            category: {category_out}")
        if description:
            print(f"            {description}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="List public skills on recipes.wisechef.ai")
    parser.add_argument("--query", "-q", default="", help="Full-text search query")
    parser.add_argument("--category", "-c", default="", help="Filter by category (e.g. marketing, data)")
    args = parser.parse_args()

    api_key = os.environ.get("RECIPES_API_KEY", "")
    if not api_key:
        print("Error: RECIPES_API_KEY environment variable is not set.", file=sys.stderr)
        print("  export RECIPES_API_KEY=rec_xxxxxxxxxxxxxxxx", file=sys.stderr)
        sys.exit(1)

    list_skills(api_key, query=args.query, category=args.category)


if __name__ == "__main__":
    main()
