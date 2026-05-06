"""Pluggable scorers for /api/recall — vector + BM25 + signal combiner."""

from __future__ import annotations

import math
import re
from typing import Iterable

from sqlalchemy.orm import Session

from app.embeddings import cosine

TIER_RANK = {"free": 0, "cook": 1, "operator": 2}


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return [t for t in re.findall(r"[A-Za-z0-9]+", text.lower()) if len(t) > 1]


def score_vector(query_emb: Iterable[float], skill_emb: Iterable[float]) -> float:
    """Cosine similarity in [0, 1] (clamped — negative cosine treated as 0)."""
    raw = cosine(query_emb, skill_emb)
    if raw < 0:
        return 0.0
    return float(raw)


def _bm25_score_text(query_tokens: list[str], doc_text: str, avgdl: float = 80.0) -> float:
    """Lightweight in-process BM25 over a single document.

    Used as the SQLite fallback (Postgres path uses ``ts_rank`` directly via
    SQLAlchemy in the route). Returns a non-negative score; magnitude is
    relative — only ordering matters.
    """
    doc_tokens = _tokenize(doc_text)
    if not doc_tokens or not query_tokens:
        return 0.0
    k1 = 1.5
    b = 0.75
    dl = len(doc_tokens)
    score = 0.0
    doc_counts: dict[str, int] = {}
    for t in doc_tokens:
        doc_counts[t] = doc_counts.get(t, 0) + 1
    for q in query_tokens:
        f = doc_counts.get(q, 0)
        if f == 0:
            continue
        # IDF ~ log(N/df+1) is a constant under single-doc; collapse to 1.
        norm = 1 - b + b * (dl / max(avgdl, 1.0))
        score += (f * (k1 + 1)) / (f + k1 * norm)
    return score


def score_bm25(query: str, skill, db: Session | None = None) -> float:
    """BM25 score for a single skill row.

    On Postgres the route may pre-compute via ``ts_rank``; this helper is the
    SQLite fallback and the per-row final scorer used everywhere.
    """
    qt = _tokenize(query or "")
    if not qt:
        return 0.0
    title = getattr(skill, "title", "") or ""
    description = getattr(skill, "description", "") or ""
    related = getattr(skill, "related_skills", None) or []
    if isinstance(related, str):
        related_str = related
    else:
        try:
            related_str = " ".join(str(x) for x in related)
        except Exception:
            related_str = ""
    # Title is weighted 3x, description 1x, tags 2x — title matches dominate.
    title_score = _bm25_score_text(qt, title) * 3.0
    desc_score = _bm25_score_text(qt, description)
    tag_score = _bm25_score_text(qt, related_str) * 2.0
    return title_score + desc_score + tag_score


def combine(
    vector_score: float,
    bm25_score: float,
    tier_match: bool,
    in_cookbook: bool,
) -> float:
    """Combine signals into a final score in roughly [0, 1+].

    Final ≈ 0.6·vec + 0.4·sigmoid(bm25) — each signal is normalised to [0, 1]
    before mixing. Tier mismatch zeroes the score (caller already filtered)
    and a small cookbook boost nudges already-installed skills up.
    """
    v = max(0.0, min(1.0, float(vector_score)))
    b = float(bm25_score)
    # Squash BM25 to [0, 1] via a simple saturating curve.
    b_norm = b / (b + 2.0) if b > 0 else 0.0
    base = 0.6 * v + 0.4 * b_norm
    if not tier_match:
        return 0.0
    if in_cookbook:
        base *= 1.05
    return base
