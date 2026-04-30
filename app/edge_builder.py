"""Skill-graph derived-edge builder (Stage 2 / G16).

Pure functions + a single I/O entry point so this module can be exercised
both by pytest fixtures (in-memory SQLite) and by the production batch
script (`scripts/build_skill_edges.py`) hitting Postgres.

Three signals combine into one weight per directed pair:

    weight = 0.6 * jaccard(tags_a, tags_b)
           + 0.2 * (1 if category_a == category_b else 0)
           + 0.2 * coinstall_score(a, b)         # 0..1

Pairs below `WEIGHT_THRESHOLD` are dropped. We write both (a→b) and (b→a)
rows to keep lookups indexable on `source_slug`.

Co-install score is a Jaccard over the set of api_key_ids that have
installed each skill in the last 30 days.

The builder is idempotent: `persist_edges()` truncates and replaces.
"""
from __future__ import annotations

import json
import tomllib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from app.models import InstallEvent, Skill, SkillDerivedEdge, SkillVersion


# ── Tunables ──────────────────────────────────────────────────────────────

# Anything below 0.15 is too noisy to be useful in the rail.
# 0.15 corresponds e.g. to a small tag overlap (jaccard ~0.25) with no other
# signals, or to a same-category match plus a tiny tag overlap.
WEIGHT_THRESHOLD = 0.15

# Cap derived edges per-source so we don't bloat the table on hub skills.
PER_SOURCE_TOP_K = 25  # graph endpoint slices to ≤10; keep some headroom

# Co-install lookback window
COINSTALL_WINDOW_DAYS = 30

# Weight allocation — keep the sum at 1.0
W_JACCARD = 0.6
W_CATEGORY = 0.2
W_COINSTALL = 0.2


# ── Pure helpers ──────────────────────────────────────────────────────────

def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    """Jaccard similarity |A∩B| / |A∪B|. Empty set on either side → 0."""
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union)


def extract_tags(skill: Skill) -> list[str]:
    """Read tags from latest skill_version's skill_toml.

    Returns [] if no version, no toml, no tags key, or parse error.
    """
    if not skill.versions:
        return []
    # `versions` is ordered by created_at desc in the model relationship
    latest = skill.versions[0]
    if not latest.skill_toml:
        return []
    try:
        data = tomllib.loads(latest.skill_toml)
    except Exception:
        return []
    raw = data.get("skill", {}).get("tags", [])
    if not isinstance(raw, list):
        return []
    return [str(t).strip().lower() for t in raw if str(t).strip()]


# ── Co-install signal ─────────────────────────────────────────────────────

def _coinstall_index(
    db: Session, since: datetime
) -> dict[str, set]:
    """slug → set of api_key_ids that installed it within the window.

    api_key_id may be None for anonymous installs; we ignore those (they
    can't be used as a co-occurrence anchor without identity).
    """
    rows = (
        db.query(InstallEvent.skill_slug, InstallEvent.api_key_id)
        .filter(InstallEvent.created_at >= since)
        .filter(InstallEvent.api_key_id.isnot(None))
        .filter(InstallEvent.skill_slug.isnot(None))
        .all()
    )
    idx: dict[str, set] = defaultdict(set)
    for slug, key_id in rows:
        idx[slug].add(key_id)
    return idx


def _coinstall_score(slug_a: str, slug_b: str, idx: dict[str, set]) -> float:
    a = idx.get(slug_a) or set()
    b = idx.get(slug_b) or set()
    return jaccard(a, b)


# ── Edge construction ─────────────────────────────────────────────────────

def build_edges(db: Session) -> list[dict]:
    """Compute all edges across the public skill catalog.

    Returns directed edges as plain dicts (suitable for both ORM persistence
    and JSON serialisation in tests). Each pair (a,b) and (b,a) appears at
    most once; self-loops are skipped; non-public skills are excluded.

    We currently do an O(N²) scan because the catalog is ≤200 skills. When we
    cross 1k skills, swap to a tag-inverted-index pre-filter.
    """
    skills: list[Skill] = (
        db.query(Skill)
        .filter(Skill.is_public == True)  # noqa: E712
        .all()
    )

    # Precompute per-skill features once
    feats = []
    for s in skills:
        feats.append({
            "skill": s,
            "slug": s.slug,
            "tags": set(extract_tags(s)),
            "category": s.category,
        })

    # Co-install index (single query)
    since = datetime.now(timezone.utc) - timedelta(days=COINSTALL_WINDOW_DAYS)
    coinstall_idx = _coinstall_index(db, since)

    # Score every unordered pair, write directed rows for each side
    edges_by_source: dict[str, list[dict]] = defaultdict(list)

    for i in range(len(feats)):
        for j in range(i + 1, len(feats)):
            fa, fb = feats[i], feats[j]
            if fa["slug"] == fb["slug"]:
                continue  # belt-and-braces

            j_score = jaccard(fa["tags"], fb["tags"])
            c_score = 1.0 if (fa["category"] and fa["category"] == fb["category"]) else 0.0
            ci_score = _coinstall_score(fa["slug"], fb["slug"], coinstall_idx)

            weight = (
                W_JACCARD * j_score
                + W_CATEGORY * c_score
                + W_COINSTALL * ci_score
            )

            if weight < WEIGHT_THRESHOLD:
                continue

            signals = {
                "jaccard": round(j_score, 4),
                "category": c_score,
                "coinstall": round(ci_score, 4),
            }
            edges_by_source[fa["slug"]].append({
                "source_slug": fa["slug"],
                "target_slug": fb["slug"],
                "weight": round(weight, 4),
                "signals": signals,
            })
            edges_by_source[fb["slug"]].append({
                "source_slug": fb["slug"],
                "target_slug": fa["slug"],
                "weight": round(weight, 4),
                "signals": signals,
            })

    # Apply per-source top-K cap by weight
    final: list[dict] = []
    for source, lst in edges_by_source.items():
        lst.sort(key=lambda e: e["weight"], reverse=True)
        final.extend(lst[:PER_SOURCE_TOP_K])
    return final


def persist_edges(db: Session, edges: list[dict]) -> int:
    """Replace existing derived edges with the new set. Returns row count.

    Uses delete-then-insert for idempotency. The whole rebuild runs inside
    the caller's transaction; the batch script wraps in `with db.begin()`.
    """
    db.query(SkillDerivedEdge).delete(synchronize_session=False)
    db.flush()

    rows = []
    now = datetime.now(timezone.utc)
    for e in edges:
        rows.append(SkillDerivedEdge(
            source_slug=e["source_slug"],
            target_slug=e["target_slug"],
            weight=float(e["weight"]),
            signals=e.get("signals") or {},
            last_built_at=now,
        ))
    db.add_all(rows)
    db.flush()
    return len(rows)
