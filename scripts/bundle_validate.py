#!/usr/bin/env python3
"""bundle_validate.py — deterministic anti-phantom bundle validator (maturity_0821 D3).

The lesson of 2026-08-21: both editorial starter bundles were 100% phantom for
two months (all members is_archived=true, anonymous install printed "installed
0 skill(s)") and NOTHING caught it. This script is the gate that would have.

It works over the LIVE public API (read-only, anonymous) — same view a visitor
gets — so it can never be fooled by an internal DB state the public can't see.

Gates (all deterministic, zero LLM):
  G1 member-existence     every member slug resolves via GET /api/skills/{slug}
  G2 member-liveness      member is_public=true AND is_archived=false AND not pro-tier
                          (an editorial bundle must install anonymously)
  G3 membership           >= MIN_MEMBERS (default 2) non-archived members
  G4 install-resolution   the bundle's public install index resolves and lists
                          every member (GET /api/bundles/public/{slug}/
                          .well-known/skills/index.json — the exact surface
                          install.sh consumes)
  G5 honest-description   description is non-empty, mentions no obvious phantom
                          capability (naive noun-extraction heuristic; WARN only —
                          honest-claims aid, not NLP)
  G6 duplicate-set        member-set identical to another bundle = FAIL;
                          >= OVERLAP_WARN jaccard = WARN

Exit contract (cron-safe, matches connector_walk.py convention):
  0 = all bundles passed (warnings allowed)
  1 = at least one bundle FAILED (or the catalog itself is unreachable — never
      report OK on total failure; mirrors the connector_walk dead-walk lesson)
  2 = usage / infra error (bad args, DNS dead, non-JSON)

Usage:
  python scripts/bundle_validate.py [--base URL] [--slug SLUG ...] [--all-public]
                                    [--min-members N] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE = "https://app.loopskill.io"
MIN_MEMBERS_DEFAULT = 2
OVERLAP_WARN = 0.8  # jaccard above this → WARN duplicate-ish
_TIMEOUT = 20
_PROBE_COOLDOWN_S = 20  # anon rate-limit backoff between bundle probes

# Tier names that an anonymous visitor cannot install. Editorial starter
# bundles must be installable free — a pro member in a "starter" bundle is the
# phantom-bundle signature (June seed had exactly this).
_NON_FREE_TIERS = {"pro", "pro_plus", "cook", "operator"}

# Words that describe capabilities; used only for the WARN-grade G5 heuristic.
_CAPABILITY_WORDS = re.compile(r"[a-z][a-z_\-]{3,}")


@dataclass
class MemberReport:
    slug: str
    ok: bool
    reason: str = ""


@dataclass
class BundleReport:
    slug: str
    members: list[MemberReport] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _get(url: str) -> tuple[int, object]:
    req = urllib.request.Request(url, headers={"User-Agent": "bundle-validate/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = resp.read()
            ctype = resp.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:  # noqa: BLE001 — Rationale: report ANY transport failure as code 0 shape
        return 0, f"{type(e).__name__}: {e}"
    if "json" in ctype:
        try:
            return 200, json.loads(body)
        except json.JSONDecodeError as e:
            return -1, f"invalid JSON: {e}"
    return 200, body.decode("utf-8", "replace")


def _discover_public_bundles(base: str) -> list[str]:
    code, data = _get(f"{base}/api/bundles/discover")
    if code != 200 or not isinstance(data, dict):
        print(f"FATAL: /api/bundles/discover unreachable (code={code})", file=sys.stderr)
        raise SystemExit(2)
    out = []
    # /api/bundles/discover returns rows under the legacy "cookbooks" key
    for b in data.get("bundles") or data.get("results") or data.get("cookbooks") or []:
        slug = b.get("slug")
        if slug:
            out.append(slug)
    return out


def _public_member_slugs(base: str, bundle_slug: str) -> tuple[list[str], str | None]:
    """Member slugs exactly as install.sh sees them (public well-known index)."""
    url = f"{base}/api/bundles/public/{bundle_slug}/.well-known/skills/index.json"
    code, data = _get(url)
    if code == 429:
        time.sleep(_PROBE_COOLDOWN_S)  # anon rate limit: back off and retry ONCE
        code, data = _get(url)
    if code == 429:
        return [], f"RATE-LIMITED (probe-side; bundle NOT counted as failed)"
    if code != 200 or not isinstance(data, dict):
        return [], f"install index unreachable (code={code}: {str(data)[:120]})"
    members = []
    for entry in data.get("skills") or []:
        slug = entry.get("slug") or entry.get("skill_slug") or entry.get("name") or entry.get("dir_name")
        if slug:
            members.append(slug)
    return members, None


def _validate_bundle(base: str, slug: str, min_members: int) -> BundleReport:
    rep = BundleReport(slug=slug)
    members, err = _public_member_slugs(base, slug)
    if err:
        if err.startswith("RATE-LIMITED"):
            rep.warnings.append(f"G4: {err}")
        else:
            rep.failures.append(f"G4: {err}")
        return rep
    if len(members) < min_members:
        rep.failures.append(f"G3: {len(members)} member(s) < min {min_members}")

    code, bdata = _get(f"{base}/api/bundles/discover")
    desc = ""
    if code == 200 and isinstance(bdata, dict):
        for b in bdata.get("bundles") or bdata.get("results") or bdata.get("cookbooks") or []:
            if b.get("slug") == slug:
                desc = (b.get("description") or "").strip()
                break
    if not desc:
        rep.warnings.append("G5: no public description found (discover card missing?)")

    seen: set[str] = set()
    for m in members:
        if m in seen:
            rep.failures.append(f"G1: duplicate member '{m}' in install index")
            rep.members.append(MemberReport(m, False, "duplicate"))
            continue
        seen.add(m)
        is_federated = m.startswith("ext:")
        if is_federated:
            # Federated members are referenced by stable identity (ext:source:slug)
            # and are NOT local catalog rows — they resolve through the federation
            # filter endpoint. Identity normalization: the bundle identity keeps
            # the source's double-dash ('--') but federation slugs store single
            # dashes; and q= is a PREFIX match, so the trailing segment is the
            # reliable lookup key, with an exact-slug confirmation of any hit.
            # identity. tail = everything after 'ext:' minus the source segment:
            # ext:skills-sh:coreyhaines31--repo--skill → coreyhaines31--repo--skill
            parts = m.split(":")
            tail = parts[-1] if len(parts) >= 3 else parts[1] if len(parts) == 2 else m
            candidates = [tail.split("--")[-1], tail.replace("--", "-"), tail]
            found = False
            for q in dict.fromkeys(candidates):  # dedupe, preserve order
                code, fdata = _get(f"{base}/api/federation/filter?q={q}&limit=5")
                if code == 429:
                    time.sleep(_PROBE_COOLDOWN_S)
                    code, fdata = _get(f"{base}/api/federation/filter?q={q}&limit=5")
                found = False
                if code == 200 and isinstance(fdata, dict):
                    normalized = tail.replace("--", "-")
                    want = f"{m.split(':')[1]}-{normalized}" if len(parts) >= 3 else normalized
                    for row in fdata.get("results") or []:
                        if (row.get("slug") or "") == want or (row.get("federated_slug") or "") == want:
                            found = True
                            break
                if found:
                    break
            if not found:
                rep.failures.append(f"G1f: federated member '{m}' does not resolve in federation index")
                rep.members.append(MemberReport(m, False, "federation 0 hits"))
            else:
                rep.members.append(MemberReport(m, True))
            continue
        code, sdata = _get(f"{base}/api/skills/{m}")
        if code == 429:
            time.sleep(_PROBE_COOLDOWN_S)
            code, sdata = _get(f"{base}/api/skills/{m}")
        if code == 429:
            rep.warnings.append(f"G1: member '{m}' probe rate-limited (unverified this run)")
            rep.members.append(MemberReport(m, True, "rate-limited"))
            continue
        if code == 404 or not isinstance(sdata, dict):
            rep.failures.append(f"G1: member '{m}' does not resolve (404)")
            rep.members.append(MemberReport(m, False, "404"))
            continue
        if code != 200:
            rep.failures.append(f"G1: member '{m}' error code {code}")
            rep.members.append(MemberReport(m, False, f"http {code}"))
            continue
        tier = (sdata.get("tier") or "").lower()
        if sdata.get("is_archived"):
            rep.failures.append(f"G2: member '{m}' is ARCHIVED (phantom signature)")
        if not sdata.get("is_public", True):
            rep.failures.append(f"G2: member '{m}' is not public")
        if tier in _NON_FREE_TIERS:
            rep.failures.append(f"G2: member '{m}' is tier '{tier}' — not free-installable")
        rep.members.append(MemberReport(m, True))

    # G5 heuristic — WARN only. Flags capability nouns in the description that
    # appear in NO member title/tagline (the "CI fix" phantom-description class).
    member_text = " ".join(
        (s.get("title") or "") + " " + (s.get("tagline") or "") + " " + (s.get("description") or "")
        for s in (
            [_get(f"{base}/api/skills/{m.slug}")[1] for m in rep.members if m.ok]  # type: ignore[misc]
        )
        if isinstance(s, dict)
    ).lower()
    desc_caps = {w for w in _CAPABILITY_WORDS.findall(desc.lower())}
    member_caps = set(_CAPABILITY_WORDS.findall(member_text))
    orphans = sorted(w for w in desc_caps - member_caps if len(w) > 4 and w not in _STOPWORDS)
    if orphans:
        rep.warnings.append("G5: description mentions unbacked terms (WARN): " + ", ".join(orphans[:8]))
    return rep


_STOPWORDS = {
    "this",
    "that",
    "with",
    "from",
    "your",
    "into",
    "bundle",
    "skills",
    "skill",
    "agent",
    "agents",
    "collection",
    "curated",
    "starter",
    "starters",
    "includes",
    "tools",
    "tooling",
    "workflows",
    "setup",
    "quick",
    "every",
    "them",
    "these",
    "those",
    "using",
    "when",
    "while",
    "which",
    "makes",
    "make",
    "made",
}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic anti-phantom bundle validator")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--slug", action="append", default=[])
    ap.add_argument("--all-public", action="store_true")
    ap.add_argument("--min-members", type=int, default=MIN_MEMBERS_DEFAULT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    slugs = list(args.slug)
    if args.all_public or not slugs:
        try:
            slugs = _discover_public_bundles(args.base)
        except SystemExit:
            raise
    if not slugs:
        print("no public bundles discovered", file=sys.stderr)
        return 2

    reports = []
    for i, s in enumerate(slugs):
        if i:
            time.sleep(_PROBE_COOLDOWN_S)
        reports.append(_validate_bundle(args.base, s, args.min_members))

    # G6 across bundles (needs all reports first)
    sets = {r.slug: {m.slug for m in r.members} for r in reports if r.members}
    slugs_list = list(sets)
    for i, a in enumerate(slugs_list):
        for b in slugs_list[i + 1 :]:
            j = _jaccard(sets[a], sets[b])
            if j == 1.0:
                for r in (a, b):
                    pass
                print(f"G6 FAIL: '{a}' and '{b}' have IDENTICAL member sets")
                for r in reports:
                    if r.slug in (a, b):
                        r.failures.append(f"G6: identical member set to the other bundle")
            elif j >= OVERLAP_WARN:
                for r in reports:
                    if r.slug in (a, b):
                        r.warnings.append(f"G6: {j:.0%} member overlap with its twin (WARN)")

    failed = [r for r in reports if not r.passed]
    if args.json:
        print(
            json.dumps(
                {
                    "base": args.base,
                    "bundles_checked": len(reports),
                    "failed": len(failed),
                    "reports": [
                        {
                            "slug": r.slug,
                            "passed": r.passed,
                            "failures": r.failures,
                            "warnings": r.warnings,
                            "members": [{"slug": m.slug, "ok": m.ok, "reason": m.reason} for m in r.members],
                        }
                        for r in reports
                    ],
                },
                indent=2,
            )
        )
    else:
        for r in reports:
            status = "PASS" if r.passed else "FAIL"
            print(f"[{status}] {r.slug} ({len(r.members)} members)")
            for f in r.failures:
                print(f"    ✗ {f}")
            for w in r.warnings:
                print(f"    ⚠ {w}")
    print(f"\n{len(reports) - len(failed)}/{len(reports)} bundles passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
