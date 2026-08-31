#!/usr/bin/env python3
"""fdeloop_0808 Phase B — the judged query set.

The predecessor plan's search gate was a single ``assert "seo" in results``.
The council called it *"laughably weak"*, correctly: that assertion passes
against alphabetically-ordered output for any query whose answer happens to
sort early, which is exactly the defect it was supposed to catch.

This replaces it with a judged set: N queries, each carrying the slug a human
says is the right answer, scored on two independent axes.

**RECALL** — does the relevant slug appear AT ALL within the cut? This is the
axis the prod defect lived on: ``q=seo`` matched 676 rows, kept 25 by
alphabet, and the row slugged ``seo`` was in neither. Recall failure is
invisible to any precision metric, because the candidate set is already wrong.

**TOP-3** — does it appear in the first three? This is the axis a user feels.

Run against the live snapshot::

    python scripts/fdeloop0808_judged_search.py

Exit 0 iff recall is 100% and top-3 >= 90% (the plan §3-B gate). Anything else
exits 1 and prints the failing queries, so this doubles as the standing
predicate for the ``fdeloop0808-search-judged`` goal.

``--baseline`` re-runs the same set under the OLD ``ORDER BY title`` behaviour
so the before/after is measured on identical data rather than asserted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import or_  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import FederationHubSkill  # noqa: E402
from app.services.federation_relevance import relevance_order_clauses  # noqa: E402

# (query, expected_slug, rationale)
#
# Every expected slug was confirmed present in the live snapshot before being
# added. A judged set that expects a row the corpus lacks measures nothing.
JUDGED: list[tuple[str, str, str]] = [
    # ── exact-slug recall: the defect class, stated six ways ──
    ("seo", "seo", "exact slug; 676 matches, previously buried at ~position 400"),
    ("humanizer", "humanizer", "exact slug; logged as a REAL zero-result query on prod"),
    ("excalidraw", "excalidraw", "exact slug, diagramming"),
    ("whisper", "whisper", "exact slug, common single word"),
    ("polymarket", "polymarket", "exact slug, proper noun"),
    ("arxiv", "arxiv", "exact slug, lowercase proper noun"),
    # ── multi-segment slugs: hyphen handling ──
    ("agent-seo-engine", "agent-seo-engine", "multi-segment exact slug"),
    ("nano-banana-pro", "nano-banana-pro", "three-segment exact slug"),
    ("test-driven-development", "test-driven-development", "long multi-segment slug"),
    # ── prefix: user typed part of an identifier ──
    ("polymark", "polymarket", "slug prefix, user still typing"),
    ("excalid", "excalidraw", "slug prefix mid-word"),
    # ── intent terms: no exact slug, must still find the right family ──
    ("code review", "code-review", "intent phrase -> hyphenated slug"),
    ("pixel art", "pixel-art-processing", "intent phrase; no exact-slug row exists"),
    ("knowledge graph", "knowledge-graph", "intent phrase; slugified match beats near-misses"),
    # ── case + whitespace robustness ──
    ("SEO", "seo", "uppercase must not change recall"),
    ("  whisper  ", "whisper", "surrounding whitespace"),
    ("ArXiv", "arxiv", "mixed case proper noun"),
    # ── terms that previously lost to the alphabetical cut ──
    ("ai-seo", "ai-seo", "slug sorting late; unreachable pre-fix"),
    ("seo-geo", "seo-geo", "slug sorting late; unreachable pre-fix"),
    ("p-seo", "p-seo", "slug sorting late; unreachable pre-fix"),
    # ── description/prose recall: a REAL logged zero-result query ──
    ("copywriting", "copywriting", "exact slug; logged as a zero-result query on prod 2026-07-13"),
    ("spectrogram", "songsee", "description-only technical term"),
]

TOP_N = 3
PER_SOURCE_LIMIT = 25  # metasearch_fanout._PER_SOURCE_TOP_N — the real cut


def _matching(db, q: str):
    like = f"%{q.strip().lower()}%"
    return db.query(FederationHubSkill).filter(
        or_(
            FederationHubSkill.title.ilike(like),
            FederationHubSkill.description.ilike(like),
            FederationHubSkill.identifier.ilike(like),
            FederationHubSkill.slug.ilike(like),
        )
    )


def run_query(db, q: str, *, baseline: bool) -> tuple[list[str], float]:
    """Return (slugs, elapsed_ms) under either the new or the old ordering."""
    base = _matching(db, q)
    t0 = time.perf_counter()
    if baseline:
        rows = base.order_by(FederationHubSkill.title).limit(PER_SOURCE_LIMIT).all()
    else:
        rows = (
            base.order_by(
                *relevance_order_clauses(FederationHubSkill, q.strip().lower()),
                FederationHubSkill.title,
            )
            .limit(PER_SOURCE_LIMIT)
            .all()
        )
    elapsed = (time.perf_counter() - t0) * 1000.0
    return [r.slug for r in rows], elapsed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", action="store_true", help="score the OLD order_by(title)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    db = SessionLocal()
    results = []
    try:
        for q, expected, why in JUDGED:
            slugs, ms = run_query(db, q, baseline=args.baseline)
            pos = slugs.index(expected) + 1 if expected in slugs else None
            results.append(
                {
                    "query": q,
                    "expected": expected,
                    "rationale": why,
                    "position": pos,
                    "recall": pos is not None,
                    "top3": pos is not None and pos <= TOP_N,
                    "latency_ms": round(ms, 2),
                    "returned": slugs[:5],
                }
            )
    finally:
        db.close()

    n = len(results)
    recall = sum(r["recall"] for r in results)
    top3 = sum(r["top3"] for r in results)
    lat = sorted(r["latency_ms"] for r in results)
    p95 = lat[max(0, int(len(lat) * 0.95) - 1)]

    summary = {
        "mode": "baseline (order_by title)" if args.baseline else "relevance-ordered",
        "queries": n,
        "recall": f"{recall}/{n} ({recall / n:.0%})",
        "top3": f"{top3}/{n} ({top3 / n:.0%})",
        "p95_latency_ms": p95,
    }

    if args.json:
        print(json.dumps({"summary": summary, "results": results}, indent=2))
    else:
        print(f"== judged query set — {summary['mode']} ==")
        for r in results:
            mark = "ok  " if r["top3"] else ("RECALL-ONLY" if r["recall"] else "MISS")
            pos = f"#{r['position']}" if r["position"] else "--"
            print(f"  {mark:<12} {r['query']:<26} -> {r['expected']:<26} {pos:>5}")
        print()
        for k, v in summary.items():
            print(f"  {k}: {v}")

    if args.baseline:
        return 0  # baseline is a measurement, never a gate

    failed = recall < n or (top3 / n) < 0.90
    if failed:
        print("\nFAIL: gate requires 100% recall and >=90% top-3.")
        for r in results:
            if not r["top3"]:
                print(f"  {r['query']!r} expected {r['expected']!r}, got {r['returned']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
