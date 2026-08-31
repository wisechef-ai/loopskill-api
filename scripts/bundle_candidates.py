#!/usr/bin/env python3
"""bundle_candidates.py — mechanical bundle-candidate generator (maturity_0821 D3).

Reads the LIVE public catalog (anonymous GET /api/skills/search, paginated) and
groups free public skills into coherent candidate bundles. PURELY mechanical:
category → tag → title-token co-occurrence. No LLM anywhere — this emits the
grounded raw material a human/LLM curation pass shapes into final bundles.

Output: JSON array of candidates to stdout (or --out file):
  [{slug, name, description_draft, member_slugs, rationale}]

Constraints honored:
  - only is_public, non-archived, free-tier skills (starter bundles must
    install anonymously — the phantom-bundle lesson)
  - 3..8 members per candidate
  --max N caps candidate count (default 40)

Exit: 0 emitted >=1 candidate · 1 emitted 0 (catalog empty/unreachable) · 2 infra.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

DEFAULT_BASE = "https://app.loopskill.io"
_TIMEOUT = 20
_COOLDOWN = 3  # polite pacing between search pages (anon rate limits are real)
# legacy alias map (cook/operator are pre-Phase-5 slugs; see app/tier_labels.py)
_NON_FREE_TIERS = {"pro", "pro_plus", "cook", "operator"}
_STOP = {
    "skill",
    "skills",
    "the",
    "and",
    "for",
    "with",
    "your",
    "into",
    "from",
    "that",
    "this",
    "using",
    "use",
    "when",
    "how",
    "cli",
    "tool",
    "tools",
}

_TOKEN = re.compile(r"[a-z][a-z\-]{2,}")


def _get(url: str) -> tuple[int, object]:
    req = urllib.request.Request(url, headers={"User-Agent": "bundle-candidates/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read()
            ctype = resp.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:  # noqa: BLE001 — Rationale: transport failures collapse to code 0
        return 0, f"{type(e).__name__}: {e}"
    if "json" in ctype:
        try:
            return 200, json.loads(body)
        except json.JSONDecodeError:
            return -1, "invalid JSON"
    return 200, body.decode("utf-8", "replace")


def _fetch_catalog(base: str) -> list[dict]:
    skills: list[dict] = []
    seen_slugs: set[str] = set()
    offset = 0
    while True:
        page = offset // 50 + 1
        code, data = _get(f"{base}/api/skills/search?limit=50&page={page}")
        if code == 429:
            time.sleep(20)
            code, data = _get(f"{base}/api/skills/search?limit=50&page={page}")
        if code != 200 or not isinstance(data, dict):
            break
        rows = data.get("results") or []
        if not rows:
            break
        before = len(seen_slugs)
        for r in rows:
            if r.get("slug") and r["slug"] not in seen_slugs:
                seen_slugs.add(r["slug"])
                skills.append(r)
        if len(seen_slugs) == before:
            break  # page repeated — no new content
        offset += len(rows)
        if offset >= 400:  # catalog is 57 public skills; 400 is a hard sanity cap
            break
        time.sleep(_COOLDOWN)
    return skills


def _tokens(*texts: str) -> set[str]:
    out: set[str] = set()
    for t in texts:
        for w in _TOKEN.findall((t or "").lower()):
            if w not in _STOP and len(w) > 2:
                out.add(w)
    return out


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)[:60]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mechanical bundle-candidate generator")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    catalog = _fetch_catalog(args.base)
    usable = [
        s
        for s in catalog
        if s.get("is_public", True)
        and not (s.get("is_archived") or False)
        and (s.get("tier") or "free").lower() not in _NON_FREE_TIERS
        and s.get("slug")
    ]
    if len(usable) < 3:
        print(f"only {len(usable)} usable public free skills — cannot form candidates", file=sys.stderr)
        return 1 if len(catalog) else 2

    by_cat: dict[str, list[dict]] = defaultdict(list)
    for s in usable:
        by_cat[(s.get("category") or "general").lower()].append(s)

    # member sets used, to avoid near-duplicate candidates
    used_sets: list[set[str]] = []
    candidates: list[dict] = []

    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        rows = sorted(by_cat[cat], key=lambda r: -(r.get("install_count_total") or 0))
        # split oversized categories into coherent sub-groups by token overlap
        clusters: list[list[dict]] = []
        for row in rows:
            toks = _tokens(row.get("title") or row.get("slug") or "", row.get("description") or "")
            placed = False
            for cl in clusters:
                overlap = toks & {
                    t for r in cl for t in _tokens(r.get("title") or "", r.get("description") or "")
                }
                if len(overlap) >= 2:
                    cl.append(row)
                    placed = True
                    break
            if not placed:
                clusters.append([row])
        for cl in clusters:
            if not (3 <= len(cl) <= 8):
                continue
            members = [r["slug"] for r in cl]
            mset = set(members)
            if any(mset == u for u in used_sets):
                continue
            if any(len(mset & u) / len(mset | u) >= 0.8 for u in used_sets):
                continue
            name_parts = _tokens(" ".join(r.get("title") or r["slug"] for r in cl))
            focus = sorted(
                name_parts & _tokens(" ".join(r.get("description") or "" for r in cl)), key=len, reverse=True
            )
            name = cat.title() + (" — " + " ".join(focus[:2]) if focus else "")
            candidates.append(
                {
                    "slug": _slugify(name),
                    "name": name,
                    "description_draft": f"{len(cl)} {cat} skills: "
                    + ", ".join(members[:4])
                    + ("…" if len(members) > 4 else ""),
                    "member_slugs": members,
                    "rationale": f"category={cat}; token-cluster focus={'/'.join(focus[:3])}; members share >=2 title/description tokens",
                }
            )
            used_sets.append(mset)
            if len(candidates) >= args.max:
                break
        if len(candidates) >= args.max:
            break

    payload = json.dumps(candidates, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(payload + "\n")
        print(f"wrote {len(candidates)} candidates to {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0 if candidates else 1


if __name__ == "__main__":
    sys.exit(main())
