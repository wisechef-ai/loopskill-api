"""Local search over the ingested Hermes Hub snapshot — bounded and multi-word.

The MCP search floor (fed1004) answers from ``federation_hub_skills`` while a
live fan-out is still running. Two properties the generic hub adapter search
does not give, and this module does:

- **Bounded.** On Postgres the query runs under ``SET LOCAL statement_timeout``,
  so a slow or locked table cannot hold an agent's search open; the caller also
  waits for this thread with its own deadline.
- **Multi-word recall.** The adapter matches the whole query as ONE phrase, so
  ``code review`` never finds a row slugged ``code-review``. Here every token
  must appear somewhere (slug, title, identifier or description), then the
  shared relevance ladder orders the matches.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

STATEMENT_TIMEOUT_MS = 800
MAX_TOKENS = 6
_TOKEN_SPLIT = re.compile(r"[\s\-_/.]+")


def query_tokens(query: str | None) -> list[str]:
    """Lower-cased search tokens. ``-``/``_``/``/``/``.`` split like spaces, so
    ``code-review`` and ``code review`` search the same tokens."""
    tokens = [t for t in _TOKEN_SPLIT.split((query or "").lower()) if t]
    return tokens[:MAX_TOKENS]


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


RELAX_MIN_TOKENS = 3


def relaxable_tokens(query: str | None) -> list[str]:
    """The query's SUBJECT tokens when a relaxed (one-word-may-miss) pass is
    allowed, else ``[]``.

    ah_1010: agents ask in 3-5 words ("postgres index advisor"). Requiring every
    word returned zero rows while every 2-word subset returned plenty, and the
    zero was then logged as a false "missing skill" demand signal. Only the
    subject words count here: grammatical stopwords and the generic words every
    skill query carries ("skill", "tool", "agent") are dropped, so "postgres
    index skill" is a 2-word query and is NOT relaxed — dropping one of two
    subject words is a different search, not a looser one.
    """
    from app.services.query_coverage import GENERIC, STOPWORDS

    core = [t for t in dict.fromkeys(query_tokens(query)) if t not in STOPWORDS and t not in GENERIC]
    return core if len(core) >= RELAX_MIN_TOKENS else []


def search_hub_index_relaxed(query: str | None, *, limit: int) -> list[Any]:
    """Hub rows containing all-but-one of the query's subject words, rows that
    cover more words first. ``[]`` when the query is not relaxable."""
    core = relaxable_tokens(query)
    if not core:
        return []
    return search_hub_index(" ".join(core), limit=limit, min_match=len(core) - 1)


def search_hub_index(query: str | None, *, limit: int, min_match: int | None = None) -> list[Any]:
    """``ExternalSkill`` rows from the hub index that contain every query token,
    most relevant first. Empty query → ``[]``. Raises on DB failure (the caller
    owns degradation).

    ``min_match`` (ah_1010) loosens "every token" to "at least ``min_match``
    tokens"; rows matching more tokens sort first. ``None`` keeps the strict
    every-token contract."""
    tokens = query_tokens(query)
    if not tokens:
        return []
    from sqlalchemy import and_, case, or_, text

    from app.database import SessionLocal
    from app.models import FederationHubSkill as M
    from app.services.federation_adapters import HermesHubAdapter
    from app.services.federation_relevance import relevance_order_clauses

    db = SessionLocal()
    try:
        if db.get_bind().dialect.name == "postgresql":
            # SET LOCAL cannot take a bind parameter; the value is an int constant.
            db.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))
        per_token = [
            or_(
                M.slug.ilike(_like(t), escape="\\"),
                M.title.ilike(_like(t), escape="\\"),
                M.identifier.ilike(_like(t), escape="\\"),
                M.description.ilike(_like(t), escape="\\"),
            )
            for t in tokens
        ]
        q = " ".join(tokens)
        if min_match is None or min_match >= len(tokens):
            rows = (
                db.query(M)
                .filter(and_(*per_token))
                .order_by(*relevance_order_clauses(M, q), M.title)
                .limit(limit)
                .all()
            )
        else:
            hits = [case((cond, 1), else_=0) for cond in per_token]
            covered = hits[0]
            for hit in hits[1:]:
                covered = covered + hit
            rows = (
                db.query(M)
                .filter(covered >= max(1, int(min_match)))
                .order_by(covered.desc(), *relevance_order_clauses(M, q), M.title)
                .limit(limit)
                .all()
            )
        adapter = HermesHubAdapter()
        return [adapter._map_hub_skill(r) for r in rows]
    finally:
        try:
            db.rollback()
        finally:
            db.close()
