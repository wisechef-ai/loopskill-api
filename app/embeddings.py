"""Local sentence-transformer embeddings for skill recall.

Uses BAAI/bge-small-en-v1.5 (384-dim) loaded once on first call. Falls back
to a deterministic hash-based pseudo-embedding when sentence-transformers
is unavailable, so tests never hard-fail on missing weights.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable

EMBED_DIM = 384
MODEL_NAME = "BAAI/bge-small-en-v1.5"

_model = None
_model_load_failed = False


def _get_model():
    """Lazy singleton — load BAAI/bge-small-en-v1.5 once per process."""
    global _model, _model_load_failed
    if _model is not None or _model_load_failed:
        return _model
    try:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(MODEL_NAME)
    except Exception:
        _model_load_failed = True
        _model = None
    return _model


def _hash_embed(text: str) -> list[float]:
    """Deterministic fallback embedding for environments without ST weights.

    Splits the text into tokens and hashes each into a fixed slot, producing
    a 384-dim sparse-bag-of-words signature. Cosine on these is roughly a
    Jaccard-on-hashes signal — enough to keep BM25 from being the only path.
    """
    vec = [0.0] * EMBED_DIM
    if not text:
        return vec
    tokens = re.findall(r"[A-Za-z0-9]+", text.lower())
    for tok in tokens:
        h = int(hashlib.blake2b(tok.encode(), digest_size=8).hexdigest(), 16)
        slot = h % EMBED_DIM
        vec[slot] += 1.0
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


def embed_text(text: str) -> list[float]:
    """Return a 384-dim embedding for ``text``."""
    model = _get_model()
    if model is None:
        return _hash_embed(text or "")
    vec = model.encode(text or "", normalize_embeddings=True)
    return [float(x) for x in vec]


def embed_skill(skill) -> list[float]:
    """Embed a Skill row using title + description + related_skills tags."""
    title = getattr(skill, "title", "") or ""
    description = getattr(skill, "description", "") or ""
    related = getattr(skill, "related_skills", None) or []
    if isinstance(related, str):
        related_str = related
    else:
        try:
            related_str = ",".join(str(x) for x in related)
        except Exception:
            related_str = ""
    text = f"{title}\n\n{description}\n{related_str}"
    return embed_text(text)


def cosine(a: Iterable[float], b: Iterable[float]) -> float:
    """Cosine similarity in [-1, 1]; returns 0.0 on degenerate inputs."""
    a = list(a) if a is not None else []
    b = list(b) if b is not None else []
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def is_model_loaded() -> bool:
    """True if the real sentence-transformer model is in memory."""
    return _model is not None
